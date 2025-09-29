import json
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

server = None
lastest_lidar_date = datetime.datetime.now(ZoneInfo("Asia/Tokyo"))
lastest_received_visitors_id = set()

# 陣営スコア管理
faction_scores = {"Attacker": 0, "Escort": 0}

# エンティティ管理用辞書
# プレイヤーは一意UIDで管理し、表示名（重複可）は属性として保持
players = {}  # player_uid -> Player object
visitors = {}  # visitor_id -> Visitor object
visitor_hit_id_cache = {}  # visitor_id -> set of processed hit ids (bounded)

# Visitor状態管理（ダブルカウント防止用）
visitor_score_processed = set()  # 既にスコア処理済みのvisitor_idを記録

# ゲームイベント管理
recent_events = []  # 最近のキル・ダメージイベントのリスト（クライアント配信用）
MAX_EVENTS = 50  # 保持する最大イベント数

DIGITS = 3


def round_digits(x):
    return float(f"{x:.{DIGITS}f}")


def add_game_event(event_type, shooter, target, damage=0, extra_info=None):
    """ゲームイベント（キル・ダメージ）をリストに追加"""
    global recent_events
    event = {
        "timestamp": datetime.datetime.now().isoformat(),
        "type": event_type,  # "kill" or "damage"
        "shooter": shooter,
        "target": target,
        "damage": damage,
        "extra_info": extra_info or {}
    }
    recent_events.append(event)

    # 最大数を超えたら古いものを削除
    if len(recent_events) > MAX_EVENTS:
        recent_events = recent_events[-MAX_EVENTS:]


def update_faction_score(faction, points=1):
    """陣営スコアを更新する"""
    global faction_scores
    if faction in faction_scores:
        faction_scores[faction] += points
        print(f"[FACTION SCORE] {faction} += {points} (Total: {faction_scores[faction]})")
    else:
        print(f"[WARNING] Unknown faction: {faction}")


def get_faction_scores():
    """現在の陣営スコアを取得する"""
    return faction_scores.copy()


def determine_player_faction(client_ip, preferred_faction=None):
    """
    プレイヤーの陣営をサーバー側で決定する
    
    Args:
        client_ip (str): クライアントのIPアドレス
        preferred_faction (str): クライアントが希望する陣営（参考程度、現在は無視）
    
    Returns:
        str: 決定された陣営 ('Attacker' または 'Escort')
    """
    # TODO: 将来的にIP範囲による大学内外判定を実装
    # 例: 133.xxx.xxx.xxx が慶應大学のIPの場合
    # if is_keio_university_ip(client_ip):
    #     return 'Escort'  # 大学内はEscort
    # else:
    #     return 'Attacker'  # 大学外はAttacker
    
    # デバッグ: 完全にランダムで陣営決定（クライアント申告無視）
    import random
    assigned_faction = random.choice(['Attacker', 'Escort'])
    
    print(f"[FACTION ASSIGNMENT] Client IP: {client_ip}, Preferred: {preferred_faction}, Assigned: {assigned_faction} (RANDOM)")
    return assigned_faction


def is_keio_university_ip(ip_address):
    """
    慶應大学のIPアドレスかどうかを判定する（将来実装用）
    
    Args:
        ip_address (str): IPアドレス
    
    Returns:
        bool: True if university IP, False otherwise
    """
    # TODO: 実際の大学IPレンジを設定
    # 例: return ip_address.startswith('133.')
    return False


class Entity:
    """エンティティの基底クラス - 共通プロパティとメソッドを提供"""

    def __init__(self, entity_id, x, y, hp=100):
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
    def __init__(self, uid, name, x, y, h, score=0, hp=100, faction='Attack', ip_address='unknown'):
        super().__init__(entity_id=name, x=x, y=y, hp=hp)
        self.uid = uid
        self.name = name
        self.h = h
        self.score = score
        self.player_kills = 0    # 新規: プレイヤーキル数
        self.visitor_kills = 0   # 新規: ビジターキル数
        # 暫定的な陣営設定 - 削除予定
        # TODO: より高度な陣営システムに置き換える予定
        self.faction = faction
        self.ip_address = ip_address  # デバッグ用: クライアントIPアドレス

    def update_position(self, x, y):
        self.x = round_digits(x)
        self.y = round_digits(y)
        self.last_update_time = datetime.datetime.now()
        self.version += 1

    def to_dict(self):
        """JSONシリアライゼーション用の辞書形式変換"""
        return {
            "uid": self.uid,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "h": getattr(self, 'h', 0.0),
            "score": self.score,
            "player_kills": self.player_kills,
            "visitor_kills": self.visitor_kills,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "is_alive": self.is_alive,
            "faction": self.faction,
            "ip_address": self.ip_address  # デバッグ用: IPアドレス情報
        }

    def increment_score(self):
        """スコアを1増加させる"""
        self.score += 1

    def increment_player_kills(self):
        """プレイヤーキル数を1増加させる"""
        self.player_kills += 1

    def increment_visitor_kills(self):
        """ビジターキル数を1増加させる"""
        self.visitor_kills += 1


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
        # スコア処理済みフラグ（ダブルカウント防止）
        self.score_processed = False

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
            "killed_by": self.killed_by,
            "faction": self.faction  # Escort固定だけど送信
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
    player_uid = getattr(connection, 'user_uid', str(uuid.uuid4()))
    print(f"New connection established from {player_name} (uid={player_uid})")

    # プレイヤーオブジェクトを作成してグローバル辞書に追加
    if hasattr(connection, 'user_name'):
        # クライアント指定があればそれを優先、無ければNone
        player_faction = getattr(connection, 'user_faction', None)
        player_ip = getattr(connection, 'user_ip', 'unknown')
        player = Player(player_uid, connection.user_name,
                        connection.user_x, connection.user_y, 5.0, faction=player_faction, ip_address=player_ip)
        players[player_uid] = player
        # 接続先にのみwelcomeメッセージでUIDと確定陣営を通知
        try:
            welcome_message = {
                "welcome": {
                    "player_uid": player_uid,
                    "assigned_faction": player_faction
                }
            }
            await connection.send(json.dumps(welcome_message))
        except Exception as e:
            print(f"Failed to send welcome message: {e}")
        print(
            f"Player {connection.user_name} joined the game with faction {player_faction} (uid={player_uid})")

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
            if hasattr(connection, 'user_uid') and connection.user_uid in players:
                player = players[connection.user_uid]
                old_x, old_y = player.x, player.y
                new_x, new_y = loaded["x"], loaded["y"]

                # 座標更新の有効性チェック
                if abs(new_x) < 0.1 and abs(new_y) < 0.1:
                    print(
                        f"WARNING: Player {player.name} sent invalid position ({new_x}, {new_y}), keeping old position ({old_x}, {old_y})")
                else:
                    player.update_position(new_x, new_y)
                    print(
                        f"Player {player.name} position updated: ({old_x}, {old_y}) -> ({player.x}, {player.y})")

                # 高さhが送られてきた場合は更新
                try:
                    if "h" in loaded:
                        player.h = round_digits(float(loaded["h"]))
                except Exception:
                    pass

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
                            killer_name = getattr(
                                connection, 'user_name', 'Unknown')

                            # プレイヤーが存在しない場合は作成
                            # 旧仕様のvisitors_being_touched経路は今後廃止予定
                            # UID導入後はこの経路ではプレイヤー作成しない

                            # プレイヤーのスコアとキル数を増加
                            players[killer_name].increment_score()
                            # ビジターキル数増加
                            players[killer_name].increment_visitor_kills()
                            connection.user_score = players[killer_name].score

                            # 旧式キル処理でも陣営スコア更新（後方互換性）
                            killer_faction = players[killer_name].faction
                            if killer_faction == 'Attacker' or killer_faction == 'Attack':
                                update_faction_score('Attacker', 1)
                                visitors[visitor_id].score_processed = True

                            print(f"[VISITOR KILL] {killer_name} killed Visitor {visitor_id} "
                                  f"(Visitor kills: {players[killer_name].visitor_kills})")

                            # イベント記録
                            add_game_event("kill", killer_name, f"Visitor_{visitor_id}",
                                           extra_info={"target_type": "visitor", "killer_visitor_kills": players[killer_name].visitor_kills})
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
                                target_faction = getattr(
                                    target_entity, 'faction', None)
                                print(
                                    f"Processing damage to Visitor {target_id} (faction: {target_faction})")
                        except ValueError:
                            # プレイヤーUIDとして処理
                            if target_id_str in players:
                                target_entity = players[target_id_str]
                                target_faction = getattr(
                                    target_entity, 'faction', None)
                                print(
                                    f"Processing damage to Player uid={target_id_str} name={target_entity.name} (faction: {target_faction})")
                            else:
                                print(
                                    f"Invalid damage report: target not found '{target_id_str}' (available player uids: {list(players.keys())})")
                                continue

                        if target_entity is None:
                            print(
                                f"Invalid damage report: target not found '{target_id_str}'")
                            continue

                        # 冪等: 既処理hit_idは無視（Visitor用キャッシュ）
                        if isinstance(target_entity, Visitor):
                            cache = visitor_hit_id_cache.setdefault(
                                target_id, set())
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
                            print(
                                f"Friendly fire rejected: shooter={shooter_id}({shooter_faction}) -> target={target_id_str}({target_faction}) dmg={damage}")
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

                            # UID導入後はサーバ発行以外のプレイヤーを作成しない

                            # スコア加算とキル数増加
                            players[shooter_id].increment_score()
                            # キルタイプを判別してカウンター増加
                            if isinstance(target_entity, Visitor):
                                # ビジターキル
                                players[shooter_id].increment_visitor_kills()
                                # Attackerによるビジターキル → Attacker陣営スコア加算
                                shooter_faction = players[shooter_id].faction
                                if shooter_faction == 'Attacker' or shooter_faction == 'Attack':
                                    update_faction_score('Attacker', 1)
                                    target_entity.score_processed = True
                            elif isinstance(target_entity, Player):
                                # プレイヤーキル
                                players[shooter_id].increment_player_kills()
                        # ダメージログ表示とイベント記録
                        if (not after_alive) and before_alive:
                            # キル発生時の詳細ログ
                            if isinstance(target_entity, Visitor):
                                killer_player = players.get(shooter_id)
                                visitor_kills = killer_player.visitor_kills if killer_player else "?"
                                print(f"[VISITOR KILL] uid={shooter_id} killed Visitor {target_id_str} "
                                      f"(dmg={damage}) [Visitor kills: {visitor_kills}]")

                                # イベント記録
                                add_game_event("kill", shooter_id, f"Visitor_{target_id_str}", damage,
                                               {"target_type": "visitor", "killer_visitor_kills": visitor_kills})

                            elif isinstance(target_entity, Player):
                                killer_player = players.get(shooter_id)
                                player_kills = killer_player.player_kills if killer_player else "?"
                                print(f"[PLAYER KILL] uid={shooter_id} killed Player uid={target_id_str} "
                                      f"(dmg={damage}) [Player kills: {player_kills}]")

                                # イベント記録
                                add_game_event("kill", shooter_id, target_id_str, damage,
                                               {"target_type": "player", "killer_player_kills": player_kills})
                        else:
                            # 通常のダメージログ
                            print(f"[DAMAGE] {shooter_id} -> {target_id_str} "
                                  f"(dmg={damage}, hp={target_entity.hp}/{target_entity.max_hp})")

                            # ダメージイベント記録
                            target_type = "visitor" if isinstance(
                                target_entity, Visitor) else "player"
                            add_game_event("damage", shooter_id, target_id_str, damage,
                                           {"target_type": target_type, "remaining_hp": target_entity.hp})
                    except Exception as e:
                        print(f"Invalid damage report: {e}")

            # # diffsフィールドの処理（Unity側から送信される追加データ）
            # if "diffs" in loaded and loaded["diffs"] is not None:
            #     print(
            #         f"Received {len(loaded['diffs'])} entity diffs from {connection.user_name}")

            await asyncio.sleep(0.01)
    finally:
        # プレイヤーが切断したときの清理処理
        if hasattr(connection, 'user_uid') and connection.user_uid in players:
            left = players.pop(connection.user_uid)
            print(
                f"Player {left.name} left the game (uid={connection.user_uid})")


async def broadcast_json(lidar2person_queue):
    global lastest_lidar_date
    global lastest_received_visitors_id
    while True:
        try:
            lidar2person = lidar2person_queue.get(False)
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue

        loaded = json.loads(lidar2person)

        now_lidar_date = datetime.datetime.fromisoformat(
            loaded["latest_timestamp"])
        if now_lidar_date < lastest_lidar_date:
            # ダミーデータがリセットされた場合、すべてのvisitorを復活
            for visitor in visitors.values():
                visitor.revive()
                visitor.killed_by = None
        lastest_lidar_date = now_lidar_date

        lastest_received_visitors_id = {x["id"] for x in loaded["objects"]}

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

        # Visitor自然消滅検知（Escort陣営スコア加算）
        # 前回存在したが今回のLidarデータにないVisitorを検出
        previous_visitor_ids = set(visitors.keys())
        current_visitor_ids = set(current_visitors.keys())
        disappeared_visitor_ids = previous_visitor_ids - current_visitor_ids
        
        for disappeared_id in disappeared_visitor_ids:
            disappeared_visitor = visitors[disappeared_id]
            # killed_byがNone（Attackerに殺されていない）かつスコア未処理の場合
            if disappeared_visitor.killed_by is None and not disappeared_visitor.score_processed:
                # Escort陣営にスコア加算（自然消滅）
                update_faction_score('Escort', 1)
                disappeared_visitor.score_processed = True
                print(f"[NATURAL DISAPPEARANCE] Visitor {disappeared_id} naturally disappeared -> Escort +1")

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

        # プレイヤー情報をオブジェクトから取得（有効座標のみ）
        players_list = []
        for player in players.values():
            player_dict = player.to_dict()
            # 無効座標（0,0,0付近）のプレイヤーはブロードキャストしない
            if abs(player_dict['x']) < 0.1 and abs(player_dict['y']) < 0.1:
                print(
                    f"Skipping player {player.name}: invalid position ({player_dict['x']}, {player_dict['y']})")
                continue
            print(
                f"Broadcasting player {player.name}: pos=({player_dict['x']}, {player_dict['y']})")
            players_list.append(player_dict)

        sending_data = {
            "lidar_time": loaded["latest_timestamp"],
            "Attacker": faction_scores["Attacker"],  # 陣営スコア: Attacker
            "Escort": faction_scores["Escort"],      # 陣営スコア: Escort
            "untouched_visitors": alive_visitors,    # Unityクライアント互換性のため
            "visitors": visitors_full,               # 新: version/HP/生死
            "players": players_list,
            # 最新10件のイベント
            "recent_events": recent_events[-10:] if recent_events else [],
        }
        broadcast(server.connections, json.dumps(sending_data))

        await asyncio.sleep(0.001)


def process_request(connection, request):
    if request.path == "/healthz":
        return connection.respond(http.HTTPStatus.OK, "OK\n")

    try:
        # Unityクライアントからのヘッダーチェック
        if "digitaltwin-user-name" not in request.headers or request.headers["digitaltwin-user-name"] == "":
            return connection.respond(http.HTTPStatus.BAD_REQUEST, "Missing user name\n")

        connection.user_name = request.headers["digitaltwin-user-name"]
        # 初期座標を取得（有効性チェック付き）
        user_x_str = request.headers.get("digitaltwin-user-x", "0")
        user_y_str = request.headers.get("digitaltwin-user-y", "0")
        connection.user_x = float(user_x_str)
        connection.user_y = float(user_y_str)

        # 初期座標が無効（0,0付近）の場合は警告
        if abs(connection.user_x) < 0.1 and abs(connection.user_y) < 0.1:
            print(
                f"WARNING: Player {connection.user_name} connecting with invalid initial position ({connection.user_x}, {connection.user_y})")
        # サーバ発行の一意UIDを割当
        connection.user_uid = str(uuid.uuid4())
        
        # クライアントIPアドレスを取得
        client_ip = getattr(connection, 'remote_address', ['unknown'])[0] if hasattr(connection, 'remote_address') else 'unknown'
        
        # クライアントからの陣営希望（参考程度）
        preferred_faction = request.headers.get("digitaltwin-preferred-faction", "").strip()
        
        # サーバー側で最終的な陣営を決定
        assigned_faction = determine_player_faction(client_ip, preferred_faction)
        connection.user_faction = assigned_faction
        connection.user_ip = client_ip  # デバッグ用: IPアドレスをconnectionに保存
    except ValueError as e:
        print(f"Invalid coordinate values: {e}")
        return connection.respond(http.HTTPStatus.BAD_REQUEST, "Invalid coordinate values\n")
    except Exception as e:
        print(f"Request processing error: {e}")
        return connection.respond(http.HTTPStatus.BAD_REQUEST, "Invalid Request\n")

    connection.user_score = 0


async def websocket_async(lidar2person_queue):
    global server
    async with serve(handler, port=8000, process_request=process_request) as s:
        server = s
        await broadcast_json(lidar2person_queue)


def main(lidar2person_queue):
    asyncio.run(websocket_async(lidar2person_queue))
