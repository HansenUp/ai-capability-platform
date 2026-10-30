/** Static guidance only: HTTP status never proves a write outcome. */
export function a2aFailure(status: number): string {
 const hints: Record<number, string> = {
  401: "凭据无效或已过期；重新应用同主体凭据，再只读核对原回执。",
  403: "当前身份或权限不允许此操作；不要切换他人身份接管登记。",
  404: "当前身份无法取得该记录；不据此判断未执行或解锁。",
  409: "状态竞争或证据冲突；只读核对原回执，不重复执行、撤销或ACK。",
  503: "服务或证据暂不可用；保持锁定，恢复可用后由本人只读核对。",
 };
 return `HTTP ${status}；${hints[status] ?? "请求未成功；先只读核对，不能推断写入结果。"}`;
}
