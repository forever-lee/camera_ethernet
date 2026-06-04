# LubanCat Edge

鲁班猫边缘端代码目录。

计划职责：

- 读取车载以太网摄像头 RTSP 视频流。
- 使用 OpenCV 采集、缩放和 JPEG 编码。
- 按单包二进制协议封装元数据和 JPEG 数据。
- 通过 WebSocket 上传到云端后端 `/ws/edge/upload`。
- 支持摄像头异常、网络异常和云端断连后的自动重连。
