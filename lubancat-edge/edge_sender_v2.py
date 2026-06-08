import cv2
import time
import json
import struct
import asyncio
import threading
import traceback
from typing import Optional, Tuple

import websockets


# =========================================================
# 边缘端配置
# =========================================================

CAMERA_IP = "192.168.4.88"
RTSP_PORT = 8554
RTSP_URL = f"rtsp://{CAMERA_IP}:{RTSP_PORT}/main"

# 本机测试：
CLOUD_WS_URL = "ws://2cb0536d.r18.cpolar.top/ws/edge/upload"

# cpolar http 映射示例：
# CLOUD_WS_URL = "ws://你的cpolar域名/ws/edge/upload"

# cpolar https 映射示例：
# CLOUD_WS_URL = "wss://你的cpolar域名/ws/edge/upload"

# 局域网云端示例：
# CLOUD_WS_URL = "ws://192.168.1.100:8000/ws/edge/upload"

EDGE_ID = "vehicle_edge_001"


# =========================================================
# 视频传输参数
# =========================================================

# 均衡推荐：画质、延迟、带宽比较平衡
SEND_FPS = 8
ENABLE_RESIZE = True
TARGET_WIDTH = 640
TARGET_HEIGHT = 360

# 低带宽推荐：
# SEND_FPS = 5
# ENABLE_RESIZE = True
# TARGET_WIDTH = 480
# TARGET_HEIGHT = 270

# 高画质推荐：
# SEND_FPS = 10
# ENABLE_RESIZE = True
# TARGET_WIDTH = 854
# TARGET_HEIGHT = 480


# JPEG 质量策略
# 每隔 KEYFRAME_INTERVAL 帧发送一次较高质量帧，其余帧发送普通质量帧。
# 注意：这不是 H.264 真正意义上的 I/P 帧，只是 MJPEG 下的“伪关键帧质量策略”。
KEYFRAME_INTERVAL = 12
KEYFRAME_JPEG_QUALITY = 62
NORMAL_JPEG_QUALITY = 42

# 自适应质量
ENABLE_ADAPTIVE_QUALITY = True

# 目标码率，单位 kbps
# 弱网建议 400~800；局域网可以 1200~2500
TARGET_KBPS = 800

MIN_JPEG_QUALITY = 28
MAX_JPEG_QUALITY = 70


# =========================================================
# 重连和稳定性参数
# =========================================================

CAMERA_RECONNECT_INTERVAL = 2
CLOUD_RECONNECT_INTERVAL = 3

MAX_READ_FAILS = 10
MAX_NO_FRAME_SECONDS = 5

# WebSocket 单包最大大小
WS_MAX_SIZE = 8 * 1024 * 1024


# =========================================================
# 摄像头读取线程
# =========================================================

class CameraReader:
    def __init__(self, rtsp_url: str):
        self.rtsp_url = rtsp_url

        self.cap: Optional[cv2.VideoCapture] = None
        self.running = False
        self.connected = False

        self.latest_frame = None
        self.latest_frame_id = 0
        self.last_frame_time = 0.0

        self.read_fail_count = 0
        self.reconnect_count = 0
        self.last_error = ""

        self.source_width = 0
        self.source_height = 0
        self.source_fps = 0.0

        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None

    def start(self):
        if self.running:
            return

        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self._release_cap()

    def get_latest_frame(self) -> Tuple[Optional[any], int, float]:
        with self.lock:
            if self.latest_frame is None:
                return None, 0, 0.0

            return self.latest_frame.copy(), self.latest_frame_id, self.last_frame_time

    def status(self):
        with self.lock:
            age = time.time() - self.last_frame_time if self.last_frame_time else None

            return {
                "connected": self.connected,
                "latest_frame_id": self.latest_frame_id,
                "last_frame_age": age,
                "read_fail_count": self.read_fail_count,
                "reconnect_count": self.reconnect_count,
                "last_error": self.last_error,
                "source_width": self.source_width,
                "source_height": self.source_height,
                "source_fps": self.source_fps,
                "target_width": TARGET_WIDTH if ENABLE_RESIZE else self.source_width,
                "target_height": TARGET_HEIGHT if ENABLE_RESIZE else self.source_height,
            }

    def _make_capture(self):
        print("正在打开 RTSP:", self.rtsp_url)

        # 使用 FFmpeg 后端
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)

        # 降低 OpenCV 内部缓存，尽量低延迟
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not cap.isOpened():
            self.connected = False
            self.last_error = "cap.isOpened() == False"
            print("RTSP 打开失败:", self.rtsp_url)
            return None

        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)

        self.source_width = width
        self.source_height = height
        self.source_fps = fps

        self.connected = True
        self.last_error = ""
        self.read_fail_count = 0

        print("RTSP 打开成功")
        print(f"原始分辨率: {width}x{height}, FPS: {fps}")

        return cap

    def _release_cap(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass

        self.cap = None
        self.connected = False

    def _loop(self):
        while self.running:
            try:
                if self.cap is None or not self.cap.isOpened():
                    self._release_cap()
                    self.cap = self._make_capture()

                    if self.cap is None:
                        self.reconnect_count += 1
                        time.sleep(CAMERA_RECONNECT_INTERVAL)
                        continue

                ret, frame = self.cap.read()

                if not ret or frame is None:
                    self.read_fail_count += 1
                    self.last_error = f"cap.read() failed {self.read_fail_count} times"
                    print(self.last_error)

                    if self.read_fail_count >= MAX_READ_FAILS:
                        print("读取失败次数过多，重连 RTSP")
                        self.reconnect_count += 1
                        self._release_cap()
                        time.sleep(CAMERA_RECONNECT_INTERVAL)

                    continue

                self.read_fail_count = 0
                now = time.time()

                if ENABLE_RESIZE:
                    frame = cv2.resize(frame, (TARGET_WIDTH, TARGET_HEIGHT), interpolation=cv2.INTER_AREA)

                # 边缘端水印，确认画面来自边缘端
                ts_text = time.strftime("%Y-%m-%d %H:%M:%S")
                cv2.putText(
                    frame,
                    f"EDGE {EDGE_ID}  {ts_text}",
                    (8, 24),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (0, 255, 0),
                    2,
                )

                with self.lock:
                    self.latest_frame = frame
                    self.latest_frame_id += 1
                    self.last_frame_time = now

                self.connected = True

            except Exception as e:
                self.last_error = str(e)
                self.connected = False
                self.reconnect_count += 1

                print("摄像头读取线程异常:", e)
                traceback.print_exc()

                self._release_cap()
                time.sleep(CAMERA_RECONNECT_INTERVAL)

        self._release_cap()
        print("摄像头读取线程退出")


# =========================================================
# 编码和单包二进制协议
# =========================================================

def encode_jpeg(frame, quality: int) -> Optional[bytes]:
    ok, buf = cv2.imencode(
        ".jpg",
        frame,
        [
            int(cv2.IMWRITE_JPEG_QUALITY),
            int(quality),
            int(cv2.IMWRITE_JPEG_OPTIMIZE),
            1,
        ],
    )

    if not ok:
        return None

    return buf.tobytes()


def pack_binary_frame(meta: dict, jpeg_bytes: bytes) -> bytes:
    """
    单包二进制协议：

    4 字节：meta JSON 长度，大端 uint32
    N 字节：meta JSON
    剩余：JPEG bytes
    """
    meta_bytes = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    header = struct.pack("!I", len(meta_bytes))
    return header + meta_bytes + jpeg_bytes


# =========================================================
# WebSocket 上传逻辑
# =========================================================

async def upload_loop(camera: CameraReader):
    last_sent_frame_id = 0

    adaptive_normal_quality = NORMAL_JPEG_QUALITY
    adaptive_key_quality = KEYFRAME_JPEG_QUALITY

    while True:
        try:
            print("正在连接云端:", CLOUD_WS_URL)

            async with websockets.connect(
                CLOUD_WS_URL,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=WS_MAX_SIZE,

                # JPEG 已经是压缩数据，关闭 WebSocket 压缩可降低 CPU 和延迟
                compression=None,
            ) as ws:
                print("云端 WebSocket 连接成功")

                hello_msg = {
                    "type": "hello",
                    "edge_id": EDGE_ID,
                    "rtsp_url": RTSP_URL,
                    "timestamp": time.time(),
                    "send_fps": SEND_FPS,
                    "transport": "single_binary_packet_meta_plus_jpeg",
                    "resize": {
                        "enabled": ENABLE_RESIZE,
                        "width": TARGET_WIDTH,
                        "height": TARGET_HEIGHT,
                    },
                    "keyframe_interval": KEYFRAME_INTERVAL,
                    "target_kbps": TARGET_KBPS,
                    "normal_quality": adaptive_normal_quality,
                    "key_quality": adaptive_key_quality,
                }

                await ws.send(json.dumps(hello_msg, ensure_ascii=False))

                frame_interval = 1.0 / SEND_FPS

                sent_count = 0
                sent_bytes_in_window = 0
                last_stat_time = time.time()
                upload_start_time = time.time()

                while True:
                    loop_start = time.time()

                    frame, frame_id, frame_time = camera.get_latest_frame()

                    if frame is None:
                        print("等待摄像头首帧...")
                        await asyncio.sleep(0.3)
                        continue

                    # 没有新帧则不上传旧帧
                    if frame_id == last_sent_frame_id:
                        await asyncio.sleep(0.005)
                        continue

                    last_sent_frame_id = frame_id

                    is_keyframe = frame_id % KEYFRAME_INTERVAL == 0
                    quality = adaptive_key_quality if is_keyframe else adaptive_normal_quality

                    jpeg_bytes = encode_jpeg(frame, quality)

                    if jpeg_bytes is None:
                        print("JPEG 编码失败")
                        await asyncio.sleep(0.05)
                        continue

                    meta = {
                        "type": "frame",
                        "edge_id": EDGE_ID,
                        "frame_id": frame_id,
                        "timestamp": time.time(),
                        "camera_frame_time": frame_time,
                        "jpeg_size": len(jpeg_bytes),
                        "is_keyframe": is_keyframe,
                        "jpeg_quality": quality,
                        "width": frame.shape[1],
                        "height": frame.shape[0],
                        "camera_status": camera.status(),
                    }

                    packet = pack_binary_frame(meta, jpeg_bytes)

                    await ws.send(packet)

                    sent_count += 1
                    sent_bytes_in_window += len(packet)

                    now = time.time()
                    stat_elapsed = now - last_stat_time

                    if stat_elapsed >= 2:
                        current_kbps = sent_bytes_in_window * 8 / stat_elapsed / 1000
                        current_fps = sent_count / stat_elapsed

                        print(
                            f"上传统计: fps≈{current_fps:.1f}, "
                            f"码率≈{current_kbps:.1f} kbps, "
                            f"累计上传时间={now - upload_start_time:.1f}s, "
                            f"质量 normal/key={adaptive_normal_quality}/{adaptive_key_quality}, "
                            f"最近帧={len(jpeg_bytes)} bytes, "
                            f"{'KEY' if is_keyframe else 'NORMAL'}"
                        )

                        if ENABLE_ADAPTIVE_QUALITY:
                            if current_kbps > TARGET_KBPS * 1.25:
                                adaptive_normal_quality = max(
                                    MIN_JPEG_QUALITY,
                                    adaptive_normal_quality - 4,
                                )
                                adaptive_key_quality = max(
                                    MIN_JPEG_QUALITY + 6,
                                    adaptive_key_quality - 4,
                                )

                            elif current_kbps < TARGET_KBPS * 0.65:
                                adaptive_normal_quality = min(
                                    MAX_JPEG_QUALITY - 10,
                                    adaptive_normal_quality + 2,
                                )
                                adaptive_key_quality = min(
                                    MAX_JPEG_QUALITY,
                                    adaptive_key_quality + 2,
                                )

                        sent_count = 0
                        sent_bytes_in_window = 0
                        last_stat_time = now

                    elapsed = time.time() - loop_start
                    sleep_time = max(0, frame_interval - elapsed)
                    await asyncio.sleep(sleep_time)

        except Exception as e:
            print("云端 WebSocket 异常:", e)
            print(f"{CLOUD_RECONNECT_INTERVAL} 秒后重连云端...")
            await asyncio.sleep(CLOUD_RECONNECT_INTERVAL)


async def main():
    camera = CameraReader(RTSP_URL)
    camera.start()

    try:
        await upload_loop(camera)
    finally:
        camera.stop()


if __name__ == "__main__":
    asyncio.run(main())