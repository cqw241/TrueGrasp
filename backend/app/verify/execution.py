"""Run synchronous math tools off the event loop with a bounded wait.

SymPy has no general cancellation: a pathological ``simplify``/``integrate`` call
can hold the event loop forever (see ``docs/reviews/OPT-05.md``).  We run each call
in a daemon thread and wait for it with a timeout, so a single calculation can no
longer stall HTTP/SSE for every other user.  The thread itself cannot be killed;
the timeout only releases the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import threading
from collections.abc import Callable
from typing import Any

from app.config import get_settings
from app.contracts.verify import Distinctness, Equivalence, ItemCheck, MathTools

logger = logging.getLogger(__name__)

MATH_UNAVAILABLE = "数学工具超时或不可用"


def _name(fn: Callable[..., Any]) -> str:
    return getattr(fn, "__qualname__", repr(fn))


async def run_with_timeout[T](
    fn: Callable[..., T], *args: Any, timeout: float | None = None, **kwargs: Any
) -> T:
    """Call a synchronous ``fn`` in a daemon thread; raise on error or timeout.

    Only values that are safe to hand to another thread may be passed in; never
    pass a SQLAlchemy ``Session`` or any other ORM object.
    """
    limit = get_settings().math_tool_timeout if timeout is None else timeout
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def deliver(result: T | None, error: Exception | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)  # type: ignore[arg-type]

    def work() -> None:
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # forwarded to the awaiting coroutine
            outcome: tuple[T | None, Exception | None] = (None, exc)
        else:
            outcome = (result, None)
        # The event loop may already be closed; the result is then unobservable.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(deliver, *outcome)

    threading.Thread(target=work, name="math-tool", daemon=True).start()
    return await asyncio.wait_for(future, timeout=limit)


async def run_math[T](
    fn: Callable[..., T],
    *args: Any,
    timeout: float | None = None,
    fallback: T | None = None,
    **kwargs: Any,
) -> T | None:
    """Like :func:`run_with_timeout`, but turn timeouts/failures into ``fallback``.

    Callers use an inconclusive fallback (``equivalent=None``/``ok=None``) so the
    model can continue teaching instead of the whole turn failing.
    """
    try:
        return await run_with_timeout(fn, *args, timeout=timeout, **kwargs)
    except TimeoutError:
        logger.warning("math tool timed out: %s", _name(fn))
        return fallback
    except Exception:
        logger.exception("math tool failed: %s", _name(fn))
        return fallback


class AsyncMathTools:
    """Async facade over a synchronous ``MathTools`` implementation.

    Inconclusive results follow the existing contract: the caller falls back to
    model judgement/explanation.  A timed-out fingerprint uses a deterministic
    fallback hash (never an empty value) so dedupe code keeps working.
    """

    def __init__(self, tools: MathTools, *, timeout: float | None = None) -> None:
        self._tools = tools
        self.timeout = timeout

    async def check_equivalent(
        self, a: str, b: str, mode: Any, variables: list[str]
    ) -> Equivalence:
        return await run_math(
            self._tools.check_equivalent,
            a,
            b,
            mode,
            variables,
            timeout=self.timeout,
            fallback=Equivalence(equivalent=None, method="none", detail=MATH_UNAVAILABLE),
        )

    async def verify_item(self, spec: Any, answer: str) -> ItemCheck:
        return await run_math(
            self._tools.verify_item,
            spec,
            answer,
            timeout=self.timeout,
            fallback=ItemCheck(ok=None, detail=MATH_UNAVAILABLE),
        )

    async def check_distinct(self, spec: Any, correct: str, predictions: list[Any]) -> Distinctness:
        return await run_math(
            self._tools.check_distinct,
            spec,
            correct,
            predictions,
            timeout=self.timeout,
            fallback=Distinctness(ok=None, detail=MATH_UNAVAILABLE),
        )

    async def fingerprint(self, spec: Any, *, preserve_symbols: bool = False) -> str:
        fallback = "unverified:" + hashlib.sha256(spec.model_dump_json().encode()).hexdigest()
        kwargs = {"preserve_symbols": True} if preserve_symbols else {}
        return await run_math(
            self._tools.fingerprint,
            spec,
            timeout=self.timeout,
            fallback=fallback,
            **kwargs,
        )
