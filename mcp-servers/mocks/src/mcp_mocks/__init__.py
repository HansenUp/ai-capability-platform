"""S8 e2e 用 mock 连接器：knowledge（8092）、crm（8093）、mail（8094）。

与 mcp-template 共用鉴权（OBO + act 断言，``mcp_template.auth``）与写规范 manifest（``mcp_template.spec``）；
宿主机运行，端口只绑 127.0.0.1：``python -m mcp_mocks knowledge|crm|mail [--port N]``。
"""

PORTS = {"knowledge": 8092, "crm": 8093, "mail": 8094}
