# 桌面端（Tauri 2 + Python Sidecar）— Phase 4（S17–S18）

- 前端复用 `apps/portal-web` 的聊天视图；`src-tauri/` 以 sidecar 方式启动本地 Runtime（`TOPOLOGY=hybrid|standalone`）。
- 通道只出不进：本地节点主动向 Console 注册 / 心跳 / 拉指令；只监听 127.0.0.1。
- 本地沙箱：microsandbox / bubblewrap；策略引擎判定结果本地缓存，断网降级自治。
- 打包：`pyinstaller` 打包 `services/runtime` 为 sidecar 二进制，`tauri build` 产出安装包。

## 当前隔离验证（F8）

`isolated/session.mjs` 与 `tests/session.test.mjs` 为无网络、无进程启动的宿主协议替身验证。运行 `node --test apps/desktop/tests/session.test.mjs`。当前仅内存合成缓存，断网禁止新写，身份失效/切换清空；不是已完成的Tauri包。上述“断网降级自治”属于原设计目标，不能解释为已授权离线业务写。自动更新与依赖安装未启用。

### 隔离图形宿主

`isolated/index.html`提供生命周期、断网缓存、身份失效与错误状态的测试界面（纯替身，不是Tauri窗口）。用已有Playwright模块运行 `PLAYWRIGHT_MODULE=<路径> node apps/desktop/tests/browser.mjs`，复用本机Chrome，自动启动并清理随机回环静态服务器。无需安装依赖；无真实业务调用。
