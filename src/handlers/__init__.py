from . import event, checkin, roles, projects, board, chat


labelers = [event.labeler, checkin.labeler, roles.labeler, projects.labeler, board.labeler, chat.labeler]


__all__ = ("labelers",)
