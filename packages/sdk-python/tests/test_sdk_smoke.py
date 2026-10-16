"""PlatformClient 的最小冒烟测试：验证构造函数正确设置认证与主体请求头。"""

import pytest

from ai_platform_sdk import PlatformClient


@pytest.mark.asyncio
async def test_client_constructs_headers():
    """token/subject 应分别映射到 Authorization / X-AI-Subject 请求头（httpx 会将头名归一化为小写）。"""
    c = PlatformClient("http://localhost:8080", token="t", subject="human:1")  # noqa: S106
    assert c._c.headers["authorization"] == "Bearer t" and c._c.headers["x-ai-subject"] == "human:1"
    await c.aclose()
