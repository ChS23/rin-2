from . import event, checkin, roles, projects, board


labelers = [event.labeler, checkin.labeler, roles.labeler, projects.labeler, board.labeler]


__all__ = ("labelers",)
