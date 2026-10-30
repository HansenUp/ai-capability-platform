# F8 最小原生工程候选（未构建）

本目录独立于原src-tauri与隔离图形宿主；不加入工作区自动构建。仅静态页面+显式受限命令占位，所有命令失败关闭；不包含shell/updater插件、externalBin、进程执行或正式身份。

直接依赖精确固定：tauri 2.12.5、tauri-build 2.7.4、serde 1.0.229。来源与授权门禁见docs/decisions/2026-10-30-F8最小原生候选与N3依赖清单.md。版本存在性已核对，G0已解析并生成Cargo.lock与392包预期校验清单，尚未下载或编译；完整锁包含跨平台项，不代表当前平台最小构建集合。

不要运行cargo build/check/run/fetch或tauri build；分别需要元数据解析、下载、构建和启动授权。当前已完成JSON/TOML静态结构与G0元数据解析；详见g0-evidence。真实调用者来源/握手/OS归属/N2窗口关闭仍应移植已批准契约并实际验证，当前占位统一拒绝命令。

## G1更新

392个锁定归档已在批准的独立Library/Caches目录完成下载及磁盘读回校验，未解包、安装或构建。证据见g1-size-evidence/download-result.json和readback-result.json。此缓存不代表Cargo已可直接构建，G2解包/脚本执行与构建需单独授权。
