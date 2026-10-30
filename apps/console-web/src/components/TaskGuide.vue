<template>
  <section aria-label="任务体验指南" class="task-guide">
    <h3>从任务到结果</h3>
    <ol>
      <li><strong>选择任务：</strong>Console 配置并发布 Agent；Portal 从可用 Agent 进入工作台。两端均受服务端权限约束。</li>
      <li><strong>新任务：</strong>应用身份、核对写账本，登记并显式执行一次；已有任务使用 Run ID 定位并只读读取。</li>
      <li><strong>隔离会话：</strong>选择会话与终态历史，合成 txt 先预览扫描，再登记、执行及本人确认；翻页会清空历史选择。</li>
      <li><strong>处理待办：</strong>按当前权限处理审批或补充输入；未知写结果先只读恢复，终结回执由原操作者显式确认。</li>
      <li><strong>核对结果：</strong>分别查看 Run 状态、工具结果和助手文本。Console 可进入 Run 调试页，Portal 在当前工作台查看同 Run 轨迹。</li>
    </ol>
    <p>重新加载或切换身份后须重新应用身份并读取；SSE 结束不代表任务成功。每次创建仍是独立 Run；隔离会话只显式引用选定历史，不自动续聊。</p>
  </section>
</template>
<style scoped>
.task-guide { border:1px solid #cbd5e1; border-radius:10px; padding:16px; margin:16px 0; overflow-wrap:anywhere; }
h3 { margin-top:0; } ol { padding-left:24px; } li { margin:8px 0; }
</style>
