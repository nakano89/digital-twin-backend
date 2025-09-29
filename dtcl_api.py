# DTCL Platform 動体情報取得API（リアルタイム）によりvista-p90-2からpersonの情報を取得

# sample.pyのコピーを変更する形で記述


"""APIから取得した情報を表示し続けるプログラム"""

import json
import sys
import time
import os  ########## 追記 ##########
from pprint import pprint
from uuid import uuid4
# from dotenv import load_dotenv

import boto3
from awscrt import auth, io, mqtt
from awscrt.exceptions import AwsCrtError
from awsiot import mqtt_connection_builder

# load_dotenv()


# Refresh Token
refresh_token = os.environ["DTCL_REFRESH_TOKEN"]  ########## 変更 ##########
# Region
region = os.environ["DTCL_REGION"]  ########## 変更 ##########
# User Pool ID
user_pool_id = os.environ["DTCL_USER_POOL_ID"]  ########## 変更 ##########
# User Pool Client ID
user_pool_client_id = os.environ["DTCL_USER_POOL_CLIENT_ID"]  ########## 変更 ##########
# Identity Pool ID
identity_pool_id = os.environ["DTCL_IDENTITY_POOL_ID"]  ########## 変更 ##########
# Endpoint
endpoint = os.environ["DTCL_ENDPOINT"]  ########## 変更 ##########

message_topic = "object/lidar/vista-p90-2/person"  # 受信するトピックを指定    ########## 変更 ##########
# message_topic = "object/lidar/+/vehicle"  # 車両のみ受信する場合
# message_topic = "object/lidar/vista-p90-1/+"  # 丁字路付近のみ受信する場合
client_id = "sample-" + str(
    uuid4()
)  # MQTTのクライアントID。他ユーザー含め重複していると接続できない。


lidar2person_queue = None  ########## 追記 ##########


def fetch_id_token(
    refresh_token: str, user_pool_client_id: str, region: str = "ap-northeast-1"
) -> str:
    """ID Token（1時間で失効）を取得する関数

    Refresh Tokenを用いて認証を行う。

    References:
        https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cognito-idp/client/initiate_auth.html
    """
    client = boto3.client("cognito-idp", region_name=region)
    response: dict = client.initiate_auth(
        AuthFlow="REFRESH_TOKEN_AUTH",
        AuthParameters={"REFRESH_TOKEN": refresh_token},
        ClientId=user_pool_client_id,
    )
    return response["AuthenticationResult"]["IdToken"]


def fetch_identity_id(
    id_token: str,
    user_pool_id: str,
    identity_pool_id: str,
    region: str = "ap-northeast-1",
) -> str:
    """Identity ID（不変）を取得する関数

    ID Tokenを用いてログインする。

    References:
        https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cognito-identity/client/get_id.html
    """
    client = boto3.client("cognito-identity", region_name=region)
    response = client.get_id(
        IdentityPoolId=identity_pool_id,
        Logins={f"cognito-idp.{region}.amazonaws.com/{user_pool_id}": id_token},
    )
    return response["IdentityId"]


def on_connection_interrupted(
    connection: mqtt.Connection, error: AwsCrtError, **kwargs
) -> None:
    """Callback invoked whenever the MQTT connection is lost.

    The MQTT client will automatically attempt to reconnect.

    Args:
        connection (mqtt.Connection): This MQTT Connection.
        error (AwsCrtError): Exception which caused connection loss.

    References:
        https://aws.github.io/aws-iot-device-sdk-python-v2/awsiot/mqtt_connection_builder.html
    """
    print(f"Connection interrupted. error: {error}")


def on_connection_resumed(
    connection: mqtt.Connection,
    return_code: mqtt.ConnectReturnCode,
    session_present: bool,
    **kwargs,
) -> None:
    """Callback invoked whenever the MQTT connection is automatically resumed.

    Args:
        connection (mqtt.Connection): This MQTT Connection.
        return_code (mqtt.ConnectReturnCode): Connect return code received from the server.
        session_present (bool): True if resuming existing session. False if new session.
            Note that the server has forgotten all previous subscriptions if this is False.
            Subscriptions can be re-established via `resubscribe_existing_topics()`.

    References:
        https://aws.github.io/aws-iot-device-sdk-python-v2/awsiot/mqtt_connection_builder.html
    """
    print(
        "Connection resumed. "
        f"return_code: {return_code} session_present: {session_present}"
    )


def on_message_received(
    topic: str, payload: bytes, dup: bool, qos: mqtt.QoS, retain: bool, **kwargs
) -> None:
    """Callback when the subscribed topic receives a message.

    Args:
        topic (str): Topic receiving message.
        payload (bytes): Payload of message.
        dup (bool): DUP flag. If True, this might be re-delivery of an earlier attempt to send the message.
        qos (mqtt.QoS): Quality of Service used to deliver the message.
        retain (bool): Retain flag. If True, the message was sent as a result of a new subscription being made by the client.

    References:
        https://awslabs.github.io/aws-crt-python/api/mqtt.html#awscrt.mqtt.Connection.subscribe
    """
    # print(f"Received message from topic '{topic}'")    ########## コメントアウト ##########
    # ペイロードのJSONをデコード
    # decoded_payload = json.loads(payload)    ########## コメントアウト ##########
    # pprint(decoded_payload)    ########## コメントアウト ##########

    lidar2person_queue.put(payload.decode())  ########## 追記 ##########


def connect_and_subscribe():
    # Refresh TokenからID Token（1時間で失効）を取得
    id_token = fetch_id_token(
        refresh_token=refresh_token,
        user_pool_client_id=user_pool_client_id,
        region=region,
    )

    global identity_id
    if identity_id is None:
        # ID TokenからIdentity ID（不変）を取得
        identity_id = fetch_identity_id(
            id_token=id_token,
            user_pool_id=user_pool_id,
            identity_pool_id=identity_pool_id,
            region=region,
        )

    # 認証情報プロバイダ
    # https://awslabs.github.io/aws-crt-python/api/auth.html#awscrt.auth.AwsCredentialsProvider.new_cognito
    credentials_provider = auth.AwsCredentialsProvider.new_cognito(
        endpoint=f"cognito-identity.{region}.amazonaws.com",
        identity=identity_id,
        tls_ctx=io.ClientTlsContext(io.TlsContextOptions()),
        logins=[
            (f"cognito-idp.{region}.amazonaws.com/{user_pool_id}", id_token),
        ],
    )

    # MQTT over WebSocket
    # https://aws.github.io/aws-iot-device-sdk-python-v2/awsiot/mqtt_connection_builder.html#awsiot.mqtt_connection_builder.websockets_with_default_aws_signing
    mqtt_connection = mqtt_connection_builder.websockets_with_default_aws_signing(
        endpoint=endpoint,
        client_id=client_id,
        region=region,
        credentials_provider=credentials_provider,
        on_connection_interrupted=on_connection_interrupted,
        on_connection_resumed=on_connection_resumed,
        clean_session=False,
        reconnect_min_timeout_secs=1,
        keep_alive_secs=30,
    )

    # Connect
    connect_future = mqtt_connection.connect()
    # Future.result() waits until a result is available
    connect_future.result()
    print("Connected!")

    # Subscribe
    print("Subscribing to topic '{}'...".format(message_topic))
    subscribe_future, packet_id = mqtt_connection.subscribe(
        topic=message_topic, qos=mqtt.QoS.AT_MOST_ONCE, callback=on_message_received
    )
    try:
        subscribe_result = subscribe_future.result()
        print(f"Subscribed with QoS {subscribe_result['qos']}")

        while True:
            # メッセージを受信し続けるための無限ループ
            # **ここにデジタルツインを活用した素敵な処理を書く**
            # 周期はtime.sleepで任意に設定可能
            # 通信が切断された場合は、mqtt_connectionが再接続を試みるので例外は発生しない
            time.sleep(1)

    except Exception as e:
        print(e, file=sys.stderr)
    finally:
        # Disconnect
        print("Disconnecting...")
        disconnect_future = mqtt_connection.disconnect()
        disconnect_future.result()
        print("Disconnected!")


def main(lidar2person_queue_):  ########## 変更 ##########
    global identity_id
    identity_id = None

    global lidar2person_queue  ########## 追記 ##########
    lidar2person_queue = lidar2person_queue_  ########## 追記 ##########

    # Exponential Backoffでリトライ
    backoff_time = 1
    while True:
        try:
            connect_and_subscribe()
        except Exception as e:
            print(e, file=sys.stderr)
        time.sleep(backoff_time)
        # 最長10分間隔でリトライ
        backoff_time = min(backoff_time * 2, 600)


########## コメントアウト ##########
# if __name__ == "__main__":
#     main()