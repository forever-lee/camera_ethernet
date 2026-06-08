# camera_ethernet

车载以太网实时视频监控系统项目仓库。

## 分支说明

- **LBC 分支**：鲁班猫侧代码，上传至此分支
- **main 分支**：稳定状态代码，上传至此分支

## 项目链路

```text
车载以太网摄像头
    -> 交换机
    -> 鲁班猫边缘端
    -> 本地云端 FastAPI 后端
    -> cpolar 公网映射
    -> Web 页面
```

## 目录结构

```text
camera_web/
├── README.md
├── .gitignore
├── .env.example
├── web-backend/
├── web-frontend/
├── lubancat-edge/
├── docs/
└── tools/
```

## 目录说明

- `web-backend/`：Web 后端目录，当前保持为空，由后端成员后续设计和实现。默认技术栈为 FastAPI。
- `web-frontend/`：Web 前端目录，当前保持为空，由前端成员后续设计和实现。
- `lubancat-edge/`：鲁班猫边缘端目录，用于放置 RTSP 拉流、JPEG 编码、WebSocket 上传等代码。
- `docs/`：项目架构、通信协议、鲁班猫部署、cpolar 映射和故障排查文档。
- `tools/`：开发、调试和测试辅助工具。

## 团队提交规范

团队成员上传代码前应当写清楚 commit 日志，说明本次提交做了什么、影响了哪些模块。

推荐格式：

```text
<type>: <简短说明>
```

常用 `type`：

- `feat`：新增功能
- `fix`：修复问题
- `docs`：文档更新
- `refactor`：代码重构
- `test`：测试相关
- `chore`：依赖、脚本、配置等杂项

示例：

```text
feat: add lubancat edge sender skeleton
docs: add cpolar deployment notes
fix: handle websocket reconnect error
```

不要使用含义不清的提交信息，例如：

```text
update
test
修改
```
