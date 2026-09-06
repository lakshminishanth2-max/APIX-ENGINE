"""Pagination tuned for time-series reads.

``LIMIT/OFFSET`` degrades badly on the fare table: ``OFFSET 50000`` forces
PostgreSQL to walk and discard fifty thousand rows on every page.  Cursor
pagination over an indexed, monotonic ordering column keeps every page an index
range scan, and - unlike offsets - it cannot skip or repeat rows when new
observations land mid-pagination, which matters when an analyst is paging
through a series while a collection run is writing.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from rest_framework.pagination import CursorPagination, PageNumberPagination
from rest_framework.response import Response

__all__ = ["CursorTimeseriesPagination", "SeriesPagination"]


class CursorTimeseriesPagination(CursorPagination):
    """Default for observation-shaped endpoints."""

    page_size = 100
    max_page_size = 1_000
    page_size_query_param = "page_size"
    #: Descending recency: the newest observation is page one.
    ordering = "-observed_at"
    cursor_query_param = "cursor"


class SeriesPagination(PageNumberPagination):
    """Index series are short and are charted whole, so a page count helps."""

    page_size = 500
    max_page_size = 5_000
    page_size_query_param = "page_size"

    def get_paginated_response(self, data: Any) -> Response:
        return Response(
            OrderedDict(
                [
                    ("count", self.page.paginator.count),
                    ("pages", self.page.paginator.num_pages),
                    ("page", self.page.number),
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                    ("results", data),
                ]
            )
        )
