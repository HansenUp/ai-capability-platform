# 桌面端（Tauri 2 + Python Sidecar）— Phase 4（S17–S18）

- 前端复用 `apps/portal-web` 的聊天视图；`src-tauri/` 以 sidecar 方式启动本地 Runtime（`TOPOLOGY=hybrid|standalone`）。
- 通道只出不进：本地节点主动向 Console 注册 / 心跳 / 拉指令；只监听 127.0.0.1。
- 本地沙箱：microsandbox / bubblewrap；策略引擎判定结果本地缓存，断网降级自治。
- 打包：`pyinstaller` 打包 `services/runtime` 为 sidecar 二进制，`tauri build` 产出安装包。
