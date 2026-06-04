"""Configuration placeholders for the LubanCat edge sender."""

CAMERA_IP = "192.168.4.88"
RTSP_PORT = 8554
RTSP_URL = f"rtsp://{CAMERA_IP}:{RTSP_PORT}/main"
CLOUD_WS_URL = "ws://127.0.0.1:8000/ws/edge/upload"
EDGE_ID = "vehicle_edge_001"
