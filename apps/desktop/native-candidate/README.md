# F8 本机开发原生候选

该目录提供Tauri原生窗口、兼容合成进程模式，以及本机Keycloak + 实际隔离Runtime开发模式。真实联调请使用apps/desktop/local-dev/start_local_f8.py；开发模式绑定当前工作区和既有venv，不是自包含Runtime分发安装包。

直接依赖固定为tauri 2.12.5、tauri-build 2.7.4、serde 1.0.229；Cargo.lock及392归档保持原锁，未新增第三方包。G1缓存规则不变。构建在自有大小写敏感APFS映像中进行，默认拒绝策略禁网，使用已核验工具；产物在artifacts/，历史产物单独保留。

## 本机验证

- `node --test apps/desktop/tests/session.test.mjs apps/desktop/tests/native-bridge.test.mjs apps/desktop/tests/guarded-bridge.test.mjs`：隔离协议回归。
- `python3 apps/desktop/native-candidate/tools/check_native_protocol.py`：用既有Rust工具链验证真实合成进程、错误握手与辅助程序篡改拒绝；自动清理临时目录。
- `PLAYWRIGHT_MODULE=<已有Playwright入口> node apps/desktop/tests/native-ui.mjs`：浏览器命令替身回归，不代替原生WebView验收。
- `python3 apps/desktop/native-candidate/tools/verify_native_app.py`：对已构建并本地签名的.app执行真实WebView/IPC验收及真实进程自测；结束后应用退出。
- 构建使用`tools/run_g2b_once.py`，会创建/挂载自有APFS映像并执行受限离线编译；需要可访问系统磁盘框架的执行上下文。禁止改为不受限cargo构建来绕过失败。
- `tools/package_local_app.py`核对源码与成功构建快照、二进制/辅助程序摘要，再创建本地ad-hoc签名.app；拒绝覆盖既有.app。它不做Developer ID签名、公证或分发安装。

正常窗口仅允许本地main调用start/stop/status/close/reconcile；无通用shell/fs/network插件。辅助程序位于固定Helpers路径，启动前须与编译时嵌入的原字节一致；不接受前端路径、PID、argv或env。一次启动一个受控管道实例，停止只关闭自有管道并等待该OS Child退出；未知结果禁重启，reconcile只读。正式业务写未开放。

测试身份和合成摘要仍由页面session内核提供，不伪装为正式OIDC或实际Runtime数据。缓存仅在内存，身份切换/失效/注销/停止/关闭/重载清空。没有更新器、持久缓存、后台任务或正式身份凭据。

`--f8-webview-test`专用CLI模式启用本地验收脚本；一次性verification_report仅在该模式及本地main上下文有效，正常应用启动时拒绝。测试通过后等待自有进程退出再关闭应用。最新真实结果见g2-b-preflight/native-webview-result.json；缺失或FAIL不能视为通过。

完整范围、失败历史及正式交付缺口见docs/decisions/2026-11-02-F8原生推进与闭环记录.md。

## 本机 Keycloak + 实际隔离 Runtime 开发模式（最新）

真实联调使用`.venv/bin/python apps/desktop/local-dev/start_local_f8.py`，完整冷启动验收加`--verify`。详见`apps/desktop/local-dev/README.md`。原合成模式保留兼容，当前原生应用还包含固定venv的只读Runtime开发适配器；上文“非实际Runtime”仅指旧合成模式，不适用于此新增开发入口。开发入口不等于自包含安装包或生产接入。

为原生开发协议与完整性校验显式增加直接依赖serde_json=1.0.154、sha2=0.10.9；二者原已在锁与缓存中，未新增第三方包。仅更新第一方根包依赖列表及锁摘要，392归档版本/checksum和本机解析234节点保持。固定解释器、入口、Runtime/contract源码清单与产物摘要一同记录，打包前复核；不复用源码漂移后的历史通过结论。

提交版构建输入在`build-inputs/packages.json`和`build-inputs/native-build.json`，不再依赖本机下载回执或历史诊断清单。构建及验证工具会创建所需输出目录；g0/g1/g2证据目录、缓存与artifacts均不提交。公开归档清单保留392包的原版本、长度和SHA256，构建仍要求已有已校验缓存，不自动下载。
