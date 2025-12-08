from . import event, checkin, roles


labelers = [event.labeler, checkin.labeler, roles.labeler]


__all__ = ("labelers",)
