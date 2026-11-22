"""Numbers shown to people: one format everywhere.

``thousands(10000) == "10,000"``. Every count, limit, row number and size in messages,
pages and emails goes through this function (the templates use it as the ``thousands``
filter, app/web/templating.py; app.js has the same rule in its ``thousands``). Numbers
in HTML attributes (``maxlength``, ``value``, ``data-*``) and in files (CSV, XLSX, PDF)
are left as plain digits.
"""

from __future__ import annotations


def thousands(value: int) -> str:
    """``value`` with a comma between groups of three digits: ``1234567 -> "1,234,567"``."""
    return f"{value:,}"
