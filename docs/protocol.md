# Protocol

边缘端到云端使用 WebSocket binary message。

数据包结构：

```text
4 bytes: metadata JSON length, big-endian uint32
N bytes: metadata JSON, UTF-8
remaining bytes: JPEG image bytes
```
