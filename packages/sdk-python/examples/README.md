# Python只读业务宿主示例（F5隔离首版）

从仓库根使用现有虚拟环境运行，无需安装或下载：

```sh
.venv/bin/python packages/sdk-python/examples/readonly_host.py --scenario read
.venv/bin/python packages/sdk-python/examples/readonly_host.py --scenario locked
.venv/bin/python packages/sdk-python/examples/readonly_host.py --scenario disconnect
.venv/bin/python packages/sdk-python/examples/readonly_host.py --scenario foreign
```

只使用HTTP MockTransport，URL固定synthetic.invalid且不经过网络。程序生成临时无效测试Bearer给替身核对，不读环境凭据、数据库或配置；不允许传真实地址/令牌。输出只含合成身份、锁状态、事件类型/seq和请求路径。不是真实身份验签、真实Runtime端到端或SDK正式发布证明。

体验：read显示一条同Run持久事件；locked显示PREPARED且保留原操作恢复/ACK指引；disconnect丢弃不完整展示并保留最后观测游标；foreign显示不可用。四种情况都不创建Run、执行/确认写入、自动重连或推断Run完成。SSE done仅表示流结束，空账本也不能替旧未知写解锁。实例结束关闭两个客户端。换身份需先结束旧读取并新建实例，不在活跃实例内换令牌。

复用的现有公开入口：
- `ai_platform_sdk.workbench.WorkbenchReader`：验签身份/账本/回执GET，未知动作失败关闭。
- `ai_platform_sdk.PlatformClient.follow_run`：同Run持久SSE，调用者掌管游标与显式恢复；不是Token流。
- 写入应独立使用`ai_platform_sdk.workbench_session.WorkbenchSession`，按[既有契约](../WORKBENCH.md)准备、一次执行、本人ACK。本示例没有写入口，也不为新增F2/F4动作提供兼容承诺。

Web对应示例见[readonly-run](../../sdk-web/examples/README.md)。本轮不改生成类型、枚举默认值或序列化，不包装旧写API成自动重试。
