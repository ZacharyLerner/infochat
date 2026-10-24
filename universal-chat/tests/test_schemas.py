"""Tests for schemas.py — constraints that protect workspace settings."""
import pytest
from pydantic import ValidationError

from schemas import WorkspaceCreate, WorkspaceUpdate


# ---------------------------------------------------------------------------
# followup_count bounds (the chat UI asks for this many follow-up questions)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", [WorkspaceCreate, WorkspaceUpdate])
@pytest.mark.parametrize("count,ok", [(0, False), (1, True), (5, True), (6, False)])
def test_followup_count_bounds(model, count, ok):
    fields = {"slug": "s", "name": "N"} if model is WorkspaceCreate else {"name": "N"}
    if ok:
        assert model(**fields, followup_count=count).followup_count == count
    else:
        with pytest.raises(ValidationError):
            model(**fields, followup_count=count)


# ---------------------------------------------------------------------------
# Required identity fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fields", [
    {"name": "N"},              # missing slug
    {"slug": "s"},              # missing name
    {"slug": "", "name": "N"},  # empty slug would map to the workspaces/ root
    {"slug": "s", "name": ""},
])
def test_slug_and_name_required(fields):
    with pytest.raises(ValidationError):
        WorkspaceCreate(**fields)
