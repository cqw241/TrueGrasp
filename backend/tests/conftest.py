"""原业务测试不依赖账号；认证边界由 test_auth 单独验证。"""

import pytest

from app.auth import Principal


@pytest.fixture(autouse=True)
def business_test_identity(request, monkeypatch, tmp_path):
    if request.module.__name__.endswith("test_auth"):
        yield
        return
    import app.main

    def teacher(request):
        request.state.principal = Principal("test", "teacher", None)

    # create_app 构造时注入测试身份，不改变生产默认鉴权。
    from fastapi import Request

    teacher.__annotations__["request"] = Request
    monkeypatch.setattr(app.main, "authorize", teacher)
    monkeypatch.setenv("AUTH_DB_PATH", str(tmp_path / "auth.db"))
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
