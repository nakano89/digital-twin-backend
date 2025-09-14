# 指定されたファイルから読み込んだダミーデータを用いて「dtcl_api.py」同様に挙動


import time


def main(dummy_data_file_path, lidar2person_queue):
    with open(dummy_data_file_path) as f:
        while True:
            line = f.readline()
            if line == "":
                f.seek(0)
                continue
            lidar2person_queue.put(line)
            time.sleep(0.1)
