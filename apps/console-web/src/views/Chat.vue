<!-- 使用侧调试：Runtime 开启强制鉴权（AUTH_REQUIRED=true）后，对话请用门户（http://localhost:5173，Keycloak 登录）；
     本页只在 Runtime 开发态（AUTH_REQUIRED=false）下可直接与 demo Agent 对话。 -->
<template>
  <h2>聊天侧</h2>
  <p style="color:#646a73;font-size:12px">Runtime 强制鉴权时请到门户（http://localhost:5173）登录后对话；本页仅在开发态可用。</p>
  <ai-assistant agent-id="demo" base-url=""></ai-assistant>
</template>
