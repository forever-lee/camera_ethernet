import time
import json
import base64
import struct
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import cv2

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Query
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


# =========================================================
# 路径配置：用于远程访问 index.html
# =========================================================

CURRENT_DIR = Path(__file__).resolve().parent

# 推荐项目结构：
# project/
# ├── cloud/
# │   └── cloud_main.py
# └── frontend/
#     └── index.html
#
# 下面会自动兼容几种常见位置：
# 1. cloud_main.py 同目录下的 index.html
# 2. cloud_main.py 同目录下的 frontend/index.html
# 3. cloud_main.py 上一级目录的 frontend/index.html

FRONTEND_CANDIDATES = [
    CURRENT_DIR,
    CURRENT_DIR / "frontend",
    CURRENT_DIR.parent / "frontend",
]

FRONTEND_DIR = None
INDEX_FILE = None

for candidate in FRONTEND_CANDIDATES:
    index_path = candidate / "index.html"
    if index_path.exists():
        FRONTEND_DIR = candidate
        INDEX_FILE = index_path
        break


# =========================================================
# 云端配置
# =========================================================

EDGE_OFFLINE_TIMEOUT = 5

# 前端 MJPEG 输出间隔
# 0.03 大约 33FPS 上限；实际由边缘端发送 FPS 决定
OUTPUT_INTERVAL = 0.03

# 是否直接复用边缘端 JPEG 输出给前端
# True：最低延迟、最低 CPU、避免二次压缩损耗
# False：云端会重新编码，可用于叠加攻击帧或其他算法画框
DIRECT_OUTPUT_EDGE_JPEG = True

# 当需要云端重新编码时使用的 JPEG 质量
CLOUD_REENCODE_JPEG_QUALITY = 85


# =========================================================
# FastAPI 初始化
# =========================================================

app = FastAPI(title="Cloud Vehicle Ethernet Video Security Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 托管前端静态文件
# 访问：
# http://127.0.0.1:8000/
# 或
# https://你的cpolar域名/
# 即可打开 index.html
if FRONTEND_DIR is not None:
    app.mount(
        "/static",
        StaticFiles(directory=str(FRONTEND_DIR)),
        name="static",
    )
    print(f"前端目录已加载: {FRONTEND_DIR}")
    print(f"首页文件: {INDEX_FILE}")
else:
    print("未找到 index.html，请检查 frontend 目录位置")


# =========================================================
# 云端帧管理器
# =========================================================

class CloudFrameHub:
    def __init__(self):
        self.lock = threading.Lock()

        self.latest_frame_jpeg: Optional[bytes] = None
        self.latest_frame_bgr = None

        self.latest_frame_seq = 0

        self.edge_id = ""
        self.edge_online = False
        self.last_frame_time = 0.0
        self.last_hello_time = 0.0
        self.edge_camera_status = {}

        self.rx_frame_count = 0
        self.rx_bytes = 0
        self.last_jpeg_size = 0
        self.last_error = ""

        self.last_frame_meta = {}
        self.last_is_keyframe = False
        self.last_edge_timestamp = 0.0
        self.last_network_delay_ms = None

        self.attack_remaining_frames = 0
        self.attack_total_count = 0
        self.attack_image = None
        self.attack_mode = "idle"
        self.last_attack_time = ""

        self.start_time = time.time()

        self.condition = threading.Condition(self.lock)

    def update_hello(self, edge_id: str, hello_data=None):
        with self.condition:
            self.edge_id = edge_id
            self.edge_online = True
            self.last_hello_time = time.time()

            if hello_data is not None:
                self.last_frame_meta = {
                    "hello": hello_data
                }

            self.condition.notify_all()

    def update_frame(self, edge_id: str, jpeg_bytes: bytes, camera_status=None, frame_meta=None):
        """
        更新云端最近一帧。
        """
        arr = np.frombuffer(jpeg_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

        if img is None:
            with self.condition:
                self.last_error = "云端 JPEG 解码失败"
            return False

        now = time.time()

        edge_ts = None
        is_keyframe = False

        if frame_meta:
            edge_ts = frame_meta.get("timestamp")
            is_keyframe = bool(frame_meta.get("is_keyframe", False))

        delay_ms = None
        if isinstance(edge_ts, (int, float)) and edge_ts > 0:
            delay_ms = round((now - edge_ts) * 1000, 2)

        with self.condition:
            self.edge_id = edge_id
            self.edge_online = True

            self.latest_frame_jpeg = jpeg_bytes
            self.latest_frame_bgr = img

            self.last_frame_time = now
            self.last_edge_timestamp = edge_ts or 0.0
            self.last_network_delay_ms = delay_ms

            self.rx_frame_count += 1
            self.rx_bytes += len(jpeg_bytes)
            self.last_jpeg_size = len(jpeg_bytes)
            self.last_error = ""

            self.last_is_keyframe = is_keyframe

            if camera_status is not None:
                self.edge_camera_status = camera_status

            if frame_meta is not None:
                self.last_frame_meta = frame_meta

            self.latest_frame_seq += 1
            self.condition.notify_all()

        return True

    def status(self):
        now = time.time()

        with self.lock:
            online = (
                self.edge_online
                and self.last_frame_time > 0
                and (now - self.last_frame_time < EDGE_OFFLINE_TIMEOUT)
            )

            uptime = max(now - self.start_time, 1)
            avg_rx_fps = self.rx_frame_count / uptime
            avg_rx_kbps = self.rx_bytes * 8 / uptime / 1000

            return {
                "cloud_running": True,
                "edge_online": online,
                "edge_id": self.edge_id,

                "rx_frame_count": self.rx_frame_count,
                "rx_bytes": self.rx_bytes,
                "last_jpeg_size": self.last_jpeg_size,
                "last_frame_age_sec": round(now - self.last_frame_time, 2) if self.last_frame_time else None,

                "avg_rx_fps": round(avg_rx_fps, 2),
                "avg_rx_bitrate_kbps": round(avg_rx_kbps, 2),

                "last_is_keyframe": self.last_is_keyframe,
                "last_network_delay_ms": self.last_network_delay_ms,
                "last_frame_meta": self.last_frame_meta,
                "edge_camera_status": self.edge_camera_status,

                "attack_mode": self.attack_mode,
                "attack_remaining_frames": self.attack_remaining_frames,
                "attack_total_count": self.attack_total_count,
                "last_attack_time": self.last_attack_time,

                "direct_output_edge_jpeg": DIRECT_OUTPUT_EDGE_JPEG,

                "last_error": self.last_error,
                "uptime_sec": round(uptime, 2),
            }

    def metrics(self):
        return self.status()

    def trigger_attack(self, frames: int = 1):
        frames = max(1, min(frames, 30))

        with self.condition:
            self.attack_remaining_frames = frames
            self.attack_mode = "cloud_frame_injection_simulation"
            self.last_attack_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.condition.notify_all()

        return {
            "success": True,
            "message": f"云端已触发攻击帧插入仿真，共 {frames} 帧",
            "frames": frames,
            "mode": "cloud_frame_injection_simulation",
        }

    def set_attack_image(self, img_bgr):
        with self.condition:
            self.attack_image = img_bgr.copy()
            self.attack_mode = "custom_attack_image_ready"
            self.condition.notify_all()

        return {
            "success": True,
            "message": "云端攻击帧图片已上传",
            "shape": list(img_bgr.shape),
        }

    def clear_attack(self):
        with self.condition:
            self.attack_remaining_frames = 0
            self.attack_image = None
            self.attack_mode = "idle"
            self.condition.notify_all()

        return {
            "success": True,
            "message": "云端攻击帧已清除",
        }

    def wait_for_new_frame(self, last_seq: int, timeout: float = 2.0):
        """
        MJPEG 输出端等待新帧，避免反复输出同一帧导致浏览器端卡顿或无效流量。
        """
        with self.condition:
            if self.latest_frame_seq == last_seq:
                self.condition.wait(timeout=timeout)

            if self.latest_frame_seq == 0 or self.latest_frame_jpeg is None:
                return None, last_seq

            return self.latest_frame_seq, self.latest_frame_seq

    def _make_default_attack_frame(self, width: int, height: int):
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[:, :] = (20, 20, 160)

        cv2.rectangle(frame, (0, 0), (width - 1, height - 1), (0, 0, 255), 12)

        center_x = width // 2
        center_y = height // 2

        cv2.putText(
            frame,
            "CLOUD INJECTED FRAME",
            (max(20, center_x - 360), center_y - 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.7,
            (255, 255, 255),
            5,
        )

        cv2.putText(
            frame,
            "SIMULATED ATTACK ONLY",
            (max(20, center_x - 330), center_y + 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.3,
            (0, 255, 255),
            4,
        )

        cv2.putText(
            frame,
            time.strftime("%Y-%m-%d %H:%M:%S"),
            (30, height - 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (255, 255, 255),
            2,
        )

        return frame

    def _apply_attack_if_needed(self, frame):
        with self.condition:
            if self.attack_remaining_frames <= 0:
                return frame

            self.attack_remaining_frames -= 1
            self.attack_total_count += 1

            attack_image = None
            if self.attack_image is not None:
                attack_image = self.attack_image.copy()

        height, width = frame.shape[:2]

        if attack_image is not None:
            injected = cv2.resize(attack_image, (width, height))
        else:
            injected = self._make_default_attack_frame(width, height)

        cv2.putText(
            injected,
            f"ATTACK COUNT: {self.attack_total_count}",
            (30, 70),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (255, 255, 255),
            2,
        )

        return injected

    def get_output_jpeg(self):
        """
        输出给前端 MJPEG 的 JPEG 帧。
        """
        with self.condition:
            if self.latest_frame_jpeg is None or self.latest_frame_bgr is None:
                return None

            # 没有攻击帧时，直接复用边缘端 JPEG，最低延迟、最低 CPU
            if DIRECT_OUTPUT_EDGE_JPEG and self.attack_remaining_frames <= 0:
                return self.latest_frame_jpeg

            frame = self.latest_frame_bgr.copy()

        frame = self._apply_attack_if_needed(frame)

        ok, buf = cv2.imencode(
            ".jpg",
            frame,
            [
                int(cv2.IMWRITE_JPEG_QUALITY),
                CLOUD_REENCODE_JPEG_QUALITY,
                int(cv2.IMWRITE_JPEG_OPTIMIZE),
                1,
            ],
        )

        if not ok:
            return None

        return buf.tobytes()


hub = CloudFrameHub()


# =========================================================
# 单包二进制解析
# =========================================================

def unpack_binary_frame(packet: bytes):
    """
    新版单包二进制协议：

    4 字节：meta JSON 长度，大端 uint32
    N 字节：meta JSON
    剩余：JPEG bytes
    """
    if len(packet) < 4:
        raise ValueError("二进制包过短")

    meta_len = struct.unpack("!I", packet[:4])[0]

    if meta_len <= 0 or meta_len > 64 * 1024:
        raise ValueError(f"meta_len 异常: {meta_len}")

    if len(packet) < 4 + meta_len:
        raise ValueError("二进制包长度不足")

    meta_bytes = packet[4:4 + meta_len]
    jpeg_bytes = packet[4 + meta_len:]

    if not jpeg_bytes:
        raise ValueError("JPEG 数据为空")

    meta = json.loads(meta_bytes.decode("utf-8"))

    return meta, jpeg_bytes


# =========================================================
# WebSocket：边缘端上传入口
# =========================================================

@app.websocket("/ws/edge/upload")
async def ws_edge_upload(websocket: WebSocket):
    await websocket.accept()
    print("边缘端 WebSocket 已连接")

    pending_meta = None

    try:
        while True:
            msg = await websocket.receive()

            if "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except Exception as e:
                    print("文本 JSON 解析失败:", e)
                    continue

                msg_type = data.get("type")

                if msg_type == "hello":
                    edge_id = data.get("edge_id", "unknown_edge")
                    hub.update_hello(edge_id, hello_data=data)

                    print("收到边缘端 hello:", edge_id)
                    print("边缘端配置:", data)

                elif msg_type == "frame_meta":
                    # 兼容旧的“先 meta，后 bytes”协议
                    pending_meta = data

                elif msg_type == "frame":
                    # 兼容旧版 base64 JSON 协议
                    jpeg_b64 = data.get("jpeg_b64", "")
                    if not jpeg_b64:
                        continue

                    try:
                        jpeg_bytes = base64.b64decode(jpeg_b64)
                    except Exception as e:
                        print("base64 解码失败:", e)
                        continue

                    edge_id = data.get("edge_id", "unknown_edge")
                    camera_status = data.get("camera_status", {})

                    ok = hub.update_frame(
                        edge_id=edge_id,
                        jpeg_bytes=jpeg_bytes,
                        camera_status=camera_status,
                        frame_meta=data,
                    )

                    if not ok:
                        print("云端接收旧版 base64 帧失败")

            elif "bytes" in msg and msg["bytes"] is not None:
                packet = msg["bytes"]

                parsed_single_packet = False

                if len(packet) >= 4:
                    try:
                        meta, jpeg_bytes = unpack_binary_frame(packet)

                        edge_id = meta.get("edge_id", "unknown_edge")
                        camera_status = meta.get("camera_status", {})

                        ok = hub.update_frame(
                            edge_id=edge_id,
                            jpeg_bytes=jpeg_bytes,
                            camera_status=camera_status,
                            frame_meta=meta,
                        )

                        if not ok:
                            print("云端接收单包二进制帧失败")

                        parsed_single_packet = True

                    except Exception:
                        parsed_single_packet = False

                if parsed_single_packet:
                    pending_meta = None
                    continue

                # 兼容旧的 pending_meta + raw JPEG bytes
                if pending_meta is not None:
                    edge_id = pending_meta.get("edge_id", "unknown_edge")
                    camera_status = pending_meta.get("camera_status", {})

                    ok = hub.update_frame(
                        edge_id=edge_id,
                        jpeg_bytes=packet,
                        camera_status=camera_status,
                        frame_meta=pending_meta,
                    )

                    if not ok:
                        print("云端接收兼容二进制 JPEG 帧失败")

                    pending_meta = None

                else:
                    # 裸 JPEG 尝试
                    ok = hub.update_frame(
                        edge_id="unknown_edge",
                        jpeg_bytes=packet,
                        camera_status={},
                        frame_meta={
                            "type": "raw_binary_jpeg",
                            "jpeg_size": len(packet),
                        },
                    )

                    if not ok:
                        print("云端接收裸 JPEG 帧失败")

    except WebSocketDisconnect:
        print("边缘端 WebSocket 断开")
        with hub.condition:
            hub.edge_online = False
            hub.condition.notify_all()

    except Exception as e:
        print("WebSocket 异常:", e)
        with hub.condition:
            hub.edge_online = False
            hub.last_error = str(e)
            hub.condition.notify_all()


# =========================================================
# HTTP API
# =========================================================

@app.get("/")
def index():
    """
    远程访问首页：
    http://服务器IP:8000/
    https://你的cpolar域名/
    """
    if INDEX_FILE is not None and INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE))

    return JSONResponse(
        {
            "message": "Cloud Vehicle Ethernet Video Security Platform is running",
            "mode": "cloud_receiver",
            "error": "未找到 index.html",
            "checked_paths": [str(p / "index.html") for p in FRONTEND_CANDIDATES],
            "tip": "请把 index.html 放到 cloud_main.py 同目录，或 ../frontend/index.html",
        },
        status_code=404,
    )


@app.get("/api")
def api_root():
    """
    后台 API 根路径。
    因为 / 已经用于返回 index.html，所以保留 /api 查看后台状态。
    """
    return {
        "message": "Cloud Vehicle Ethernet Video Security Platform is running",
        "mode": "cloud_receiver",
        "transport": "single_binary_packet_meta_plus_jpeg",
        "direct_output_edge_jpeg": DIRECT_OUTPUT_EDGE_JPEG,
        "frontend_dir": str(FRONTEND_DIR) if FRONTEND_DIR else None,
        "index_file": str(INDEX_FILE) if INDEX_FILE else None,
    }


@app.get("/api/stream/status")
def stream_status():
    return JSONResponse(hub.status())


@app.get("/api/metrics")
def get_metrics():
    return JSONResponse(hub.metrics())


@app.post("/api/attack/insert-frame")
def insert_attack_frame(frames: int = Query(default=1, ge=1, le=30)):
    return JSONResponse(hub.trigger_attack(frames=frames))


@app.post("/api/attack/upload-frame")
async def upload_attack_frame(file: UploadFile = File(...)):
    content = await file.read()

    arr = np.frombuffer(content, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

    if img is None:
        return JSONResponse(
            {
                "success": False,
                "message": "图片解析失败，请上传 jpg/png/bmp 图片",
            },
            status_code=400,
        )

    return JSONResponse(hub.set_attack_image(img))


@app.post("/api/attack/clear")
def clear_attack():
    return JSONResponse(hub.clear_attack())


# =========================================================
# MJPEG 输出给前端
# =========================================================

def mjpeg_generator():
    last_seq = 0

    while True:
        new_seq, last_seq = hub.wait_for_new_frame(last_seq, timeout=2.0)

        if new_seq is None:
            time.sleep(0.05)
            continue

        frame = hub.get_output_jpeg()

        if frame is None:
            time.sleep(0.05)
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Pragma: no-cache\r\n\r\n" +
            frame +
            b"\r\n"
        )

        time.sleep(OUTPUT_INTERVAL)


@app.get("/video_feed")
def video_feed():
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# =========================================================
# 直接运行入口
# =========================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1,
    )