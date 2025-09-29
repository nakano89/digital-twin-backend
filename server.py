import json
import time
import datetime
from zoneinfo import ZoneInfo
import queue
import asyncio
import http
from websockets.asyncio.server import broadcast, serve
import uuid

# 福沢先生を原点とする
origin_lat = 35.388469
origin_lon = 139.426989
alt_offset = 33.0921

# SFCでの1メートルあたりの緯度・経度
meter_per_1lat = 0.00000901325
meter_per_1lon = 0.0000110065

lastest_bonus_time = time.monotonic()
lastest_lidar_date = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))
lastest_received_visitors_id = set()

class Visitors:
    _alive_visitors = []
    _alive_visitors_history = set()
    _visitors_hp = {}
    _dead_visitors_count = 0

    @classmethod
    def update_existing_visitors(cls, visitors_id):
        alive_visitors = [
            id_ for id_ in visitors_id if cls._visitors_hp.get(id_, 100) > 0
        ]
        cls._alive_visitors = alive_visitors
        cls._alive_visitors_history.update(set(alive_visitors))

    @classmethod
    def get_bonus(cls):
        count_visitors = max(1, len(cls._alive_visitors_history))
        bonus = int(cls._dead_visitors_count / count_visitors * 100)
        cls._alive_visitors_history = set()
        cls._dead_visitors_count = 0
        return bonus

    @classmethod
    def get_hp(cls, id_):
        if id_ not in cls._visitors_hp:
            cls._visitors_hp[id_] = 100

        return cls._visitors_hp[id_]

    @classmethod
    def try_decrease_hp(cls, id_, try_decrease):
        if id_ not in cls._visitors_hp:
            cls._visitors_hp[id_] = 100

        if id_ not in cls._alive_visitors:
            return 0

        if cls._visitors_hp[id_] <= try_decrease:
            decrease = cls._visitors_hp[id_]
            cls._visitors_hp[id_] = 0
            cls._dead_visitors_count += 1
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
                "insfc": c.user_insfc,
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
    def give_all_sfc_players_bonus(cls, bonus):
        for c in cls.server.connections:
            if c.user_insfc:
                c.user_score += bonus

    @classmethod
    def init(cls, connection, name, insfc):
        assert name != ""
        for c in cls.server.connections:
            assert name != c.user_name
        connection.user_name = name
        connection.user_insfc = insfc
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
    def try_decrease_hp(cls, name, try_decrease):
        for c in cls.server.connections:
            if c.user_name == name:
                if c.user_hp <= try_decrease:
                    decrease = c.user_hp
                    c.user_hp = 0
                    return decrease
                else:
                    c.user_hp -= try_decrease
                    return try_decrease

    @classmethod
    def increase_score(cls, connection, increase):
        connection.user_score += increase

# エンティティ管理用辞書
players = {}  # player_name -> Player object
visitors = {}  # visitor_id -> Visitor object
visitor_hit_id_cache = {}  # visitor_id -> set of processed hit ids (bounded)

DIGITS = 3


def round_digits(x):
    return float(f"{x:.{DIGITS}f}")


class Entity:
    """エンティティの基底クラス - 共通プロパティとメソッドを提供"""

    def __init__(self, entity_id, x=0, y=0, hp=100):
        self.id = entity_id
        self.x = x
        self.y = y
        self.hp = hp
        self.max_hp = hp
        self.is_alive = True
        self.last_update_time = datetime.datetime.now()
        self.version = 0
        # 陣営（faction）: 'Escort'（護衛）/ 'Attack'（攻撃）/ 追加も可
        # 既定は None。サブクラスや生成時に設定する
        self.faction = None

    def to_dict(self):
        """サブクラスでオーバーライドして、JSONシリアライゼーション用の辞書を返す"""
        raise NotImplementedError("Subclasses must implement to_dict method")

    def take_damage(self, damage):
        """ダメージを受ける"""
        before = self.hp
        self.hp = max(0, self.hp - damage)
        if self.hp <= 0:
            self.is_alive = False
        if self.hp != before:
            self.version += 1

    def revive(self, hp=None):
        """エンティティを復活させる"""
        self.hp = hp if hp is not None else self.max_hp
        self.is_alive = True


class Player(Entity):
    def __init__(self, name, x=0, y=0, score=0, hp=100, faction='Attack'):
        super().__init__(entity_id=name, x=x, y=y, hp=hp)
        self.name = name
        self.score = score
        # 暫定的な陣営設定 - 削除予定
        # TODO: より高度な陣営システムに置き換える予定
        self.faction = faction

    def update_position(self, x, y):
        self.x = round_digits(x)
        self.y = round_digits(y)
        self.last_update_time = datetime.datetime.now()
        self.version += 1

    def to_dict(self):
        """JSONシリアライゼーション用の辞書形式変換"""
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "score": self.score,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "is_alive": self.is_alive
        }

    def increment_score(self):
        """スコアを1増加させる"""
        self.score += 1


class Visitor(Entity):
    def __init__(self, id, lon, lat, alt, heading, vh, vxy, hp=100):
        # 福澤座標系に変換
        x = round_digits((lon - origin_lon) / meter_per_1lon)
        y = round_digits((lat - origin_lat) / meter_per_1lat)

        super().__init__(entity_id=int(id), x=x, y=y, hp=hp)
        self.h = round_digits(alt - alt_offset)
        self.heading = round_digits(heading)
        self.vh = round_digits(vh)
        self.vxy = round_digits(vxy)
        self.killed_by = None
        self.death_time = None
        # 暫定: Visitor は Escort 陣営
        self.faction = 'Escort'

    def to_dict(self):
        """JSONシリアライゼーション用の辞書形式変換"""
        return {
            "id": self.id,
            "x": self.x,
            "y": self.y,
            "h": self.h,
            "heading": self.heading,
            "vh": self.vh,
            "vxy": self.vxy,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "is_alive": self.is_alive,
            "killed_by": self.killed_by
        }

    def update_lidar_data(self, lon, lat, alt, heading, vh, vxy):
        """Lidarデータでvisitorの位置・状態を更新"""
        self.x = round_digits((lon - origin_lon) / meter_per_1lon)
        self.y = round_digits((lat - origin_lat) / meter_per_1lat)
        self.h = round_digits(alt - alt_offset)
        self.heading = round_digits(heading)
        self.vh = round_digits(vh)
        self.vxy = round_digits(vxy)
        self.last_update_time = datetime.datetime.now()
        # 位置更新もバージョンを進める（必要に応じて）
        self.version += 1

    def apply_kill_once(self, killer_name: str):
        if not self.is_alive:
            if self.killed_by is None:
                self.killed_by = killer_name
            return False
        self.is_alive = False
        self.hp = 0
        self.killed_by = killer_name
        self.death_time = datetime.datetime.now()
        self.version += 1
        return True

async def handler(connection):
    player_name = getattr(connection, 'user_name', 'Unknown')
    print(f"New connection established from {player_name}")

    # プレイヤーオブジェクトを作成してグローバル辞書に追加
    if hasattr(connection, 'user_name'):
        # クライアント指定があればそれを優先、無ければデフォルトAttack
        player_faction = getattr(connection, 'user_faction', None) or 'Attack'
        player = Player(connection.user_name,
                        connection.user_x, connection.user_y, faction=player_faction)
        players[connection.user_name] = player
        print(f"Player {connection.user_name} joined the game with faction {player_faction}")

    try:
        while True:
            try:
                received = await connection.recv()
            except Exception as e:
                print(f"Connection lost: {e}")
                break

            try:
                loaded = json.loads(received)
                assert type(loaded["x"]) in (int, float)
                assert type(loaded["y"]) in (int, float)
                # Unity側からは文字列配列として送信される
                for visitor in loaded["visitors_being_touched"]:
                    assert type(visitor) is str
                # diffsフィールドは存在する場合のみ処理
                if "diffs" in loaded and loaded["diffs"] is not None:
                    assert type(loaded["diffs"]) is list
                # damage_reports フィールド
                if "damage_reports" in loaded and loaded["damage_reports"] is not None:
                    assert type(loaded["damage_reports"]) is list
            except Exception as e:
                print(f"Invalid message received: {e}")
                await connection.close(code=1008, reason="Invalid Message")
                break

            # プレイヤーオブジェクトの位置を更新
            if hasattr(connection, 'user_name') and connection.user_name in players:
                player = players[connection.user_name]
                player.update_position(loaded["x"], loaded["y"])

            # connectionの属性も更新（後方互換性のため）
            connection.user_x = loaded["x"]
            connection.user_y = loaded["y"]

            # 旧: 撃破されたvisitorの処理（後方互換）
            for visitor in loaded["visitors_being_touched"]:
                try:
                    visitor_id = int(visitor)
                    # visitor_idが存在し、まだ生きているかチェック
                    if visitor_id in visitors and visitors[visitor_id].is_alive:
                        if visitors[visitor_id].apply_kill_once(getattr(connection, 'user_name', 'Unknown')):
                            # プレイヤーのスコアを増加
                            if hasattr(connection, 'user_name') and connection.user_name in players:
                                players[connection.user_name].increment_score()
                                connection.user_score = players[connection.user_name].score
                            else:
                                connection.user_score += 1
                            print(
                                f"Visitor {visitor_id} killed by {getattr(connection, 'user_name', 'Unknown')}")
                except ValueError:
                    print(f"Invalid visitor ID format: {visitor}")
                    continue

            # 新: damage_reports によるダメージ適用（冪等 + 陣営チェック）
            if "damage_reports" in loaded and loaded["damage_reports"] is not None:
                for rep in loaded["damage_reports"]:
                    try:
                        target_id_str = rep.get("target_id")
                        if not target_id_str:
                            print(f"Invalid damage report: empty target_id")
                            continue
                            
                        shooter_id = rep.get("shooter_id")
                        damage = float(rep.get("damage", 0))
                        hit_id = rep.get("hit_id") or str(uuid.uuid4())
                        # target_version = rep.get("target_version")  # 今は参照のみ

                        # ターゲットの種類を判定（Visitor または Player）
                        target_entity = None
                        target_faction = None
                        
                        # Visitor として処理を試行
                        try:
                            target_id = int(target_id_str)
                            if target_id in visitors:
                                target_entity = visitors[target_id]
                                target_faction = getattr(target_entity, 'faction', None)
                                print(f"Processing damage to Visitor {target_id} (faction: {target_faction})")
                        except ValueError:
                            # プレイヤーIDとして処理
                            if target_id_str in players:
                                target_entity = players[target_id_str]
                                target_faction = getattr(target_entity, 'faction', None)
                                print(f"Processing damage to Player {target_id_str} (faction: {target_faction})")
                            else:
                                print(f"Invalid damage report: target not found '{target_id_str}' (available players: {list(players.keys())})")
                                continue
                        
                        if target_entity is None:
                            print(f"Invalid damage report: target not found '{target_id_str}'")
                            continue
                        
                        # 冪等: 既処理hit_idは無視（Visitor用キャッシュ）
                        if isinstance(target_entity, Visitor):
                            cache = visitor_hit_id_cache.setdefault(target_id, set())
                            if hit_id in cache:
                                continue
                            cache.add(hit_id)
                            if len(cache) > 512:
                                # サイズ制限（古いものから削除）
                                try:
                                    cache.pop()
                                except KeyError:
                                    pass

                        # 暫定的な陣営ダメージ判定（プレイヤー同士でも陣営が違えばダメージ有効）
                        # TODO: より高度なダメージシステムに置き換える予定
                        shooter_faction = None
                        if shooter_id in players:
                            shooter_faction = players[shooter_id].faction
                        # ここで拡張: shooter が visitor の可能性があれば visitors も参照
                        elif shooter_id is not None:
                            try:
                                sid_int = int(shooter_id)
                                if sid_int in visitors:
                                    shooter_faction = visitors[sid_int].faction
                            except Exception:
                                shooter_faction = shooter_faction

                        # 暫定的な処理: 同陣営同士のダメージは無効（プレイヤー同士でも陣営が違えばダメージ有効）
                        if shooter_faction is not None and target_faction is not None and shooter_faction == target_faction:
                            # 同士討ちは棄却
                            print(f"Friendly fire rejected: shooter={shooter_id}({shooter_faction}) -> target={target_id_str}({target_faction}) dmg={damage}")
                            continue

                        # 既に死亡していれば無視
                        if not target_entity.is_alive:
                            continue

                        # ダメージ適用
                        before_alive = target_entity.is_alive
                        target_entity.take_damage(damage)
                        after_alive = target_entity.is_alive
                        if (not after_alive) and before_alive:
                            # 初めてのキル
                            target_entity.killed_by = shooter_id
                            target_entity.death_time = datetime.datetime.now()
                            # スコア加算
                            if shooter_id in players:
                                players[shooter_id].increment_score()
                        print(f"Damage applied: target={target_id_str}, dmg={damage}, hp={target_entity.hp}, alive={target_entity.is_alive}, by={shooter_id}")
                    except Exception as e:
                        print(f"Invalid damage report: {e}")

            # # diffsフィールドの処理（Unity側から送信される追加データ）
            # if "diffs" in loaded and loaded["diffs"] is not None:
            #     print(
            #         f"Received {len(loaded['diffs'])} entity diffs from {connection.user_name}")

            await asyncio.sleep(0.01)
    finally:
        # プレイヤーが切断したときの清理処理
        if hasattr(connection, 'user_name') and connection.user_name in players:
            del players[connection.user_name]
            print(f"Player {connection.user_name} left the game")


async def broadcast_json(lidar2person_queue):
    global lastest_bonus_time
    global lastest_lidar_date
    global lastest_received_visitors_id
    while True:
        try:
            lidar2_person = lidar2person_queue.get(False)
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue

        now_time = time.monotonic()
        if lastest_bonus_time + 30 <= now_time:
            Players.give_all_sfc_players_bonus(Visitors.get_bonus())
            lastest_bonus_time = now_time

        loaded = json.loads(lidar2_person)

        now_lidar_date = datetime.datetime.fromisoformat(
            loaded["latest_timestamp"])
        if now_lidar_date < lastest_lidar_date:
            # ダミーデータがリセットされた場合、すべてのvisitorを復活
            for visitor in visitors.values():
                visitor.revive()
                visitor.killed_by = None
        lastest_lidar_date = now_lidar_date

        Visitors.update_existing_visitors([str(o["id"]) for o in loaded["objects"]])

        # Visitorオブジェクトをグローバル辞書に保存し、to_dict()で変換
        current_visitors = {}
        for visitor_data in loaded["objects"]:
            visitor_id = visitor_data["id"]

            # 既存のvisitorがあれば更新、なければ新規作成
            if visitor_id in visitors:
                # 既存visitorのLidarデータを更新
                visitor = visitors[visitor_id]
                visitor.update_lidar_data(
                    visitor_data["position"]["lon"], visitor_data["position"]["lat"],
                    visitor_data["position"]["alt"], visitor_data["heading"],
                    visitor_data["vertical_velocity"], visitor_data["horizontal_speed"]
                )
            else:
                # 新規visitorを作成
                visitor = Visitor(visitor_id, visitor_data["position"]["lon"],
                                  visitor_data["position"]["lat"], visitor_data["position"]["alt"],
                                  visitor_data["heading"], visitor_data["vertical_velocity"],
                                  visitor_data["horizontal_speed"])

            current_visitors[visitor.id] = visitor

        # グローバルvisitors辞書を更新
        visitors.clear()
        visitors.update(current_visitors)

        # Unity側で期待されている形式 + 新形式（全訪問者）
        alive_visitors = []
        for visitor in visitors.values():
            if visitor.is_alive:
                alive_visitors.append(visitor.to_dict())

        visitors_full = []
        for visitor in visitors.values():
            visitors_full.append({
                "id": visitor.id,
                "x": visitor.x,
                "y": visitor.y,
                "h": visitor.h,
                "heading": visitor.heading,
                "vh": visitor.vh,
                "vxy": visitor.vxy,
                "hp": visitor.hp,
                "max_hp": visitor.max_hp,
                "is_alive": visitor.is_alive,
                "killed_by": visitor.killed_by,
                "version": visitor.version,
            })

        # プレイヤー情報をオブジェクトから取得
        players_list = []
        for player in players.values():
            players_list.append(player.to_dict())

        sending_data = {
            "lidar_time": loaded["latest_timestamp"],
            "untouched_visitors": alive_visitors,  # Unityクライアント互換性のため
            "visitors": visitors_full,  # 新: version/HP/生死
            "players": players_list,
        }
        broadcast(Players.server.connections, json.dumps(sending_data))

        await asyncio.sleep(0.001)


def process_request(connection, request):
    if request.path == "/healthz":
        return connection.respond(http.HTTPStatus.OK, "OK\n")

    try:
        # Unityクライアントからのヘッダーチェック
        if "digitaltwin-user-name" not in request.headers or request.headers["digitaltwin-user-name"] == "":
            return connection.respond(http.HTTPStatus.BAD_REQUEST, "Missing user name\n")

        # 既存のユーザー名重複チェック
        for c in Players.server.connections:
            if hasattr(c, 'user_name') and c.user_name == request.headers["digitaltwin-user-name"]:
                return connection.respond(http.HTTPStatus.CONFLICT, "User name already exists\n")

        connection.user_name = request.headers["digitaltwin-user-name"]
        connection.user_x = float(
            request.headers.get("digitaltwin-user-x", "0"))
        connection.user_y = float(
            request.headers.get("digitaltwin-user-y", "0"))
        # クライアントからの陣営指定（任意）
        client_faction = request.headers.get("digitaltwin-user-faction", "").strip()
        if client_faction in ("Escort", "Attack"):
            connection.user_faction = client_faction
    except ValueError as e:
        print(f"Invalid coordinate values: {e}")
        return connection.respond(http.HTTPStatus.BAD_REQUEST, "Invalid coordinate values\n")
    except Exception as e:
        print(f"Request processing error: {e}")
        return connection.respond(http.HTTPStatus.BAD_REQUEST, "Invalid Request\n")


async def websocket_async(lidar2person_queue):
    async with serve(handler, port=8000, process_request=process_request) as server:
        Players.server = server
        await broadcast_json(lidar2person_queue)


def main(lidar2person_queue):
    asyncio.run(websocket_async(lidar2person_queue))
