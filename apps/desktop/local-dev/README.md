# F8 本机开发联调

本目录把真实本机 Keycloak、原生 Tauri 窗口与隔离 Runtime 连起来。Runtime 使用项目现有 FastAPI/JWT 验签和临时 SQLite，只开放固定的只读桥接；数据是两位开发用户的合成会话，不启动业务 Worker、模型、工具、Console 节点代理或迁移。

## 启动

在项目根目录运行：

```sh
.venv/bin/python apps/desktop/local-dev/start_local_f8.py
```

启动器会核对已打包应用摘要，只启动自有 Keycloak 容器，打开原生本机开发模式；关闭应用后停止自有容器、删除独立网络及临时 realm。已有 8180 服务时拒绝启动，不接管、不重建其他服务。Keycloak 使用项目已缓存的26.3系列镜像，固定镜像ID，不自动拉取。需要 Docker Desktop 引擎已运行，以及本机现有venv和已构建.app。

操作：启动隔离 Runtime → 登录本机 Keycloak → 读取摘要 → 模拟断网/恢复在线 → 切换账号、注销或停止 → 清理并关闭。登录表单在独立、无桥接权限、非持久化的 Keycloak 窗口中；正常使用时由用户输入账号。当前用户令牌到期后需要重新登录。

公开且可销毁的本机测试账号：

| 用户 | 租户 | 临时会话数量 |
|---|---|---|
| `f8-alice` | `default` | 1 |
| `f8-bob` | `t2` | 2 |

两者密码均为 `f8-local-development-only`，只适用于启动器创建的临时本机 realm，不是正式账号。普通开发令牌120秒；自动验收实例12秒。主窗口不接收密码、access/id/refresh token或PKCE verifier。

固定开发配置：issuer `http://127.0.0.1:8180/realms/ai-platform`，client `f8-desktop`，audience `ai-runtime`，回调 `http://127.0.0.1:18765/callback`。客户端定义见keycloak-client.json，realm作用域来自项目services/identity/realm-export.json；不修改原realm或已有用户数据。

## 验收

```sh
.venv/bin/python apps/desktop/local-dev/start_local_f8.py --verify
.venv/bin/python apps/desktop/local-dev/verify_integrity.py
```

第一条从独立 Keycloak 冷启动开始，执行真实后端和真实原生窗口测试后退出并清理。只有该专用原生验收模式自动填入公开测试账号，正常开发模式不会自动填写。第二条验证入口和Runtime源码摘要不匹配时拒绝执行。证据在`apps/desktop/native-candidate/g2-b-preflight/`的local-backend-result.json、local-native-webview-result.json、local-integrity-result.json和local-idp-owner.json。退出码非0不判通过。

Native开发适配器绑定当前工作区和`.venv/bin/python`，无系统Python回退。原生侧核对解释器解析路径和二进制SHA256；固定解释器以隔离模式在同一进程内核对入口原摘要后执行同一份字节，不输出/转存源码；Runtime依赖源码清单在导入前逐项复核。修改入口、Runtime/contract源码、解释器或移动工作区后，旧产物会拒绝启动，需要按native-candidate/tools/run_g2b_once.py重建并重新打包验收。

Callback只接受精确回环Host、单次state、短时授权码和S256 PKCE，进一步验ID-token nonce/sub与Runtime access token的签名、issuer、audience和有效期。令牌仅在桥接进程内存；身份切换、注销、取消登录或过期清空缓存；断网仅允许当前身份未过期的既有内存缓存，不发新Runtime读取。原生只保留自有OS Child对象，不按PID认领或杀进程；清理未知时禁重启并保留诊断。

这是当前macOS arm64的工作区开发模式。直接双击兼容候选.app默认仍进入原合成模式；真实联调请用上述启动器。它不是自包含Runtime安装包，不包含永久缓存、业务写或自动更新。正式Python打包、生产身份/撤权与离线策略、持久存储/崩溃恢复、正式签名公证、更新信任及Windows包列入发布阶段，不计本机开发验收通过。
