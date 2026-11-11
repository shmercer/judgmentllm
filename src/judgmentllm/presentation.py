"""Assessment presentation revisions, independent of document/transport versions.

Revision 1 is the historical renderer and source-ID CJ wire. Revision 2 is the
Markdown renderer and position-alias CJ wire. Persisted omission always means 1;
new preparation selects 2. This is not a user-selectable prompt style.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator


def _version(value: object) -> object:
    if type(value) is not int:
        raise ValueError("presentation_version must be an integer")
    return value


PresentationVersion = Annotated[Literal[1, 2], BeforeValidator(_version)]
ESSAY_ALIASES = ("essay_01", "essay_02")
