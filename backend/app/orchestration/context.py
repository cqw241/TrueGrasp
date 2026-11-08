"""Private invocation context; no additional Agent input fields."""

from contextvars import ContextVar
from datetime import date

current_teaching_date: ContextVar[date | None] = ContextVar("current_teaching_date", default=None)
