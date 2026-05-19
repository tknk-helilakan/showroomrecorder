from __future__ import annotations

import asyncio
import contextvars
import functools
from datetime import timezone, timedelta
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - only used on Python 3.8
    try:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    except ImportError:  # pragma: no cover
        def ZoneInfo(name: str):  # type: ignore
            if name in {"Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin"}:
                return timezone(timedelta(hours=8), name)
            if name.upper() in {"UTC", "ETC/UTC"}:
                return timezone.utc
            raise RuntimeError(
                "Install backports.zoneinfo or use timezone Asia/Shanghai/UTC on Python 3.8"
            )


async def to_thread(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    if hasattr(asyncio, "to_thread"):
        return await asyncio.to_thread(func, *args, **kwargs)
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    call = functools.partial(context.run, func, *args, **kwargs)
    return await loop.run_in_executor(None, call)
