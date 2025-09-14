import os
from multiprocessing import Process, Queue

import dtcl_api
import dummy
import server


if __name__ == "__main__":
    lidar2person_queue = Queue()

    server_process = Process(target=server.main, args=(lidar2person_queue,))
    server_process.start()

    if os.environ["DUMMY_DATA_FILE_PATH"] == "":
        dtcl_api.main(lidar2person_queue)
    else:
        dummy.main(os.environ["DUMMY_DATA_FILE_PATH"], lidar2person_queue)
