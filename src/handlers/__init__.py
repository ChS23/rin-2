from . import event, checkin, roles, projects, board, chat
from .creative import handler as _creative  # noqa: F401 — регистрирует scheduled jobs


labelers = [event.labeler, checkin.labeler, roles.labeler, projects.labeler, board.labeler, chat.labeler]


__all__ = ("labelers",)
