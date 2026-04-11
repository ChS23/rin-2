from . import event, checkin, roles, projects, board, chat
from .creative import handler as creative


labelers = [event.labeler, checkin.labeler, roles.labeler, projects.labeler, board.labeler, chat.labeler, creative.labeler]


__all__ = ("labelers",)
