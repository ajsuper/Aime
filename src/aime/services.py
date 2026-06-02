"""View-facing data services. Wrap ToolGateway with convenience methods so
calendar/topic views (and any future frontend) don't need to know which
tool_name strings to send or how to parse the envelopes. This is where API 
calls that get stuff for the front end are handled. (Showing events in a calendar
format etc)"""

import datetime

from .tool_gateway import ToolGateway


def _events_from(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("events", []) or []
    return []


def _topics_from(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("topics", []) or []
    return []


def sort_events_by_date(events: list[dict]) -> list[dict]:
    """Sort events ascending by (date, time). Tolerates missing fields by
    pushing them to the far future so they appear last rather than crashing."""
    def key(ev):
        d, m, y = (ev.get("date") or "01/01/9999").split("/")
        hh, mm = (ev.get("time") or "00:00").split(":")
        return (int(y), int(m), int(d), int(hh), int(mm))
    return sorted(events, key=key)


class CalendarService:
    """Query helpers for the events store."""

    def __init__(self, gateway: ToolGateway):
        self._gw = gateway

    def events_for_month(self, year: int, month: int, *, include_archived: bool = False) -> list[dict]:
        """Every event with a date in the given month/year. Uses a wide-day
        range so events on day 31 still come back regardless of how the
        backend interprets day boundaries."""
        mm = f"{month:02d}"
        data = self._gw.call(
            "get_events",
            sort_order="asc",
            filter_by_date=True,
            start_date=f"00/{mm}/{year}",
            end_date=f"40/{mm}/{year}",
            archived="all" if include_archived else "active_only",
        )
        return _events_from(data)

    def events_for_day(self, year: int, month: int, day: int, *, include_archived: bool = False) -> list[dict]:
        day_str = f"{day:02d}/{month:02d}/{year}"
        data = self._gw.call(
            "get_events",
            sort_order="asc",
            filter_by_date=True,
            start_date=day_str,
            end_date=day_str,
            archived="all" if include_archived else "active_only",
        )
        return _events_from(data)

    def replace_event(
        self, event_id: int, *, title: str, summary: str, category: str,
        date: str, time: str, archived: bool,
        status: str | None = None, commitment_id: str | None = None,
        status_change_reason: str | None = None, rescheduled_from: str | None = None,
    ) -> dict:
        """Edit / archive an existing event. Mirrors the backend's
        `replace_event` tool — caller supplies the full record so a partial
        edit always sends the unchanged fields too.

        The lifecycle-metadata kwargs (status, commitment_id, …) are optional:
        only the ones passed are sent, and the backend preserves any field it
        doesn't receive, so a plain title/summary edit never resets them."""
        payload = dict(
            id=event_id,
            title=title,
            summary=summary,
            category=category,
            date=date,
            time=time,
            archived=archived,
        )
        for key, value in (
            ("status", status),
            ("commitment_id", commitment_id),
            ("status_change_reason", status_change_reason),
            ("rescheduled_from", rescheduled_from),
        ):
            if value is not None:
                payload[key] = value
        return self._gw.call("replace_event", **payload)


class TopicService:
    """Query helpers for the topics store."""

    def __init__(self, gateway: ToolGateway):
        self._gw = gateway

    def list_topics(self) -> list[dict]:
        return _topics_from(self._gw.call("get_topics"))

    def get_topic_contents(self, topic_id) -> str:
        resp = self._gw.call("get_topic_contents", id=topic_id)
        if isinstance(resp, dict):
            return resp.get("contents", "") or ""
        return ""

    def replace_topic_contents(self, topic_id, contents: str):
        return self._gw.call("replace_topic_contents", id=topic_id, contents=contents)

    def create_topic(
        self, title: str, summary: str, category: str, folder: str = ""
    ) -> dict:
        return self._gw.call(
            "create_topic",
            title=title,
            summary=summary,
            category=category,
            folder=folder,
        )

    def rename_folder(self, old_name: str, new_name: str) -> dict:
        return self._gw.call("rename_folder", old_name=old_name, new_name=new_name)

    def list_folders(self) -> list[dict]:
        resp = self._gw.call("list_folders")
        if isinstance(resp, dict):
            return resp.get("folders", []) or []
        return []
