from typing import NoReturn

from fastapi import HTTPException


def not_implemented(task: str) -> NoReturn:
    """接口桩：路由和响应模型已定，实现由对应任务的负责人完成。"""
    raise HTTPException(status_code=501, detail=f"尚未实现（{task}）")
