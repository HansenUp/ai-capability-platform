/** Guidance uses validated receipt metadata only, never raw node execution output. */
export function nodeReceiptGuidance(type: string, status: string): string {
  if (status === "pending") return "等待回执：仅证明指令已登记，不能判断是否开始或完成。查询原指令，不重新下发。";
  if (status === "error") return "执行错误：本页不推断失败原因或是否产生部分效果。保留指令 ID，核对授权、配置与节点侧证据后人工处理；不自动重试。";
  if (status === "unsupported") return "节点不支持此指令：核对节点能力、角色与启动配置；不能当作执行成功，不自动切换节点重发。";
  if (status !== "ok") return "回执状态未知：不能判断成功或解锁，请核对服务端证据。";
  if (type === "graceful_restart") return "已收到排空与重启请求回执，尚不能证明重启完成。需要新的进程 PID、boot_id 与 ready=true 的证据；当前健康快照不能替代这些证据。本页未提供进程更替证明。";
  if (type === "switch_model") return "模型路由变更回执成功，仅影响符合基线条件的新建且未固定版本的 Run；已有 Run、显式版本与审批恢复不切换。不代表已经发生真实模型调用。";
  if (type === "reload_config") return "节点配置回执成功，仅涉及当前进程的轮询与请求超时；不代表限流配置或跨重启持久化。";
  if (type === "reload_skills") return "Skill 重载回执成功；不代表业务 Run 或真实工具调用已验收。";
  return "已收到成功回执，但此指令的业务效果未在本页定义，请核对对应契约。";
}
