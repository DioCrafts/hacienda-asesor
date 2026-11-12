"""Proactive advisor: jobs that write notifications for a case without being asked."""

from hacienda_gpt.proactive.store import Notification, NotificationStore

__all__ = ["Notification", "NotificationStore"]
