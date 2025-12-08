from . import event, checkin, roles, projects


labelers = [event.labeler, checkin.labeler, roles.labeler, projects.labeler]


__all__ = ("labelers",)
