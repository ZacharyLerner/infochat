"""Tests for schemas.py — Pydantic model validation."""
import pytest
from pydantic import ValidationError

from schemas import DefaultQuestionCategory, WorkspaceCreate, WorkspaceUpdate, WorkspaceResponse


# ---------------------------------------------------------------------------
# DefaultQuestionCategory
# ---------------------------------------------------------------------------

class TestDefaultQuestionCategory:
    def test_valid_category(self):
        cat = DefaultQuestionCategory(category="Getting Started", questions=["Hi", "Hello"])
        assert cat.category == "Getting Started"
        assert cat.questions == ["Hi", "Hello"]

    def test_empty_questions_defaults(self):
        cat = DefaultQuestionCategory(category="Tips")
        assert cat.questions == []

    def test_category_too_short(self):
        with pytest.raises(ValidationError):
            DefaultQuestionCategory(category="", questions=[])

    def test_category_too_long(self):
        with pytest.raises(ValidationError):
            DefaultQuestionCategory(category="A" * 41, questions=[])

    def test_category_max_length_ok(self):
        cat = DefaultQuestionCategory(category="A" * 40, questions=[])
        assert len(cat.category) == 40


# ---------------------------------------------------------------------------
# WorkspaceCreate
# ---------------------------------------------------------------------------

class TestWorkspaceCreate:
    def test_minimal_valid(self):
        ws = WorkspaceCreate(slug="test", name="Test Workspace")
        assert ws.slug == "test"
        assert ws.name == "Test Workspace"
        assert ws.followup_enabled is False
        assert ws.followup_count == 3
        assert ws.email_enabled is False
        assert ws.welcome_text == "Send a message to get started."
        assert ws.default_questions == []

    def test_full_valid(self):
        ws = WorkspaceCreate(
            slug="admissions",
            name="Admissions Office",
            welcome_text="Ask me anything!",
            followup_enabled=True,
            followup_count=5,
            email_enabled=True,
            default_questions=[
                {"category": "Help", "questions": ["What can you do?"]}
            ],
        )
        assert ws.slug == "admissions"
        assert ws.followup_count == 5
        assert len(ws.default_questions) == 1
        assert ws.default_questions[0].category == "Help"

    def test_slug_too_short(self):
        with pytest.raises(ValidationError):
            WorkspaceCreate(slug="", name="Name")

    def test_slug_too_long(self):
        with pytest.raises(ValidationError):
            WorkspaceCreate(slug="a" * 101, name="Name")

    def test_name_too_short(self):
        with pytest.raises(ValidationError):
            WorkspaceCreate(slug="slug", name="")

    def test_name_too_long(self):
        with pytest.raises(ValidationError):
            WorkspaceCreate(slug="slug", name="N" * 201)

    def test_welcome_text_too_long(self):
        with pytest.raises(ValidationError):
            WorkspaceCreate(slug="slug", name="Name", welcome_text="W" * 301)

    def test_followup_count_too_low(self):
        with pytest.raises(ValidationError):
            WorkspaceCreate(slug="slug", name="Name", followup_count=0)

    def test_followup_count_too_high(self):
        with pytest.raises(ValidationError):
            WorkspaceCreate(slug="slug", name="Name", followup_count=6)

    def test_followup_count_boundary_min(self):
        ws = WorkspaceCreate(slug="slug", name="Name", followup_count=1)
        assert ws.followup_count == 1

    def test_followup_count_boundary_max(self):
        ws = WorkspaceCreate(slug="slug", name="Name", followup_count=5)
        assert ws.followup_count == 5


# ---------------------------------------------------------------------------
# WorkspaceUpdate
# ---------------------------------------------------------------------------

class TestWorkspaceUpdate:
    def test_valid_update(self):
        wu = WorkspaceUpdate(
            name="New Name",
            followup_enabled=True,
            followup_count=2,
        )
        assert wu.name == "New Name"
        assert wu.followup_count == 2

    def test_update_followup_count_out_of_range(self):
        with pytest.raises(ValidationError):
            WorkspaceUpdate(name="Name", followup_count=10)

    def test_update_name_too_long(self):
        with pytest.raises(ValidationError):
            WorkspaceUpdate(name="N" * 201, followup_count=3)

    def test_update_welcome_text_max(self):
        wu = WorkspaceUpdate(name="Name", welcome_text="W" * 300)
        assert len(wu.welcome_text) == 300

    def test_update_welcome_text_over_max(self):
        with pytest.raises(ValidationError):
            WorkspaceUpdate(name="Name", welcome_text="W" * 301)


# ---------------------------------------------------------------------------
# WorkspaceResponse
# ---------------------------------------------------------------------------

class TestWorkspaceResponse:
    def test_from_dict(self):
        data = {
            "slug": "test",
            "name": "Test",
            "welcome_text": "Hello",
            "followup_enabled": True,
            "followup_count": 3,
            "email_enabled": False,
            "default_questions": [],
        }
        resp = WorkspaceResponse(**data)
        assert resp.slug == "test"
        assert resp.followup_enabled is True
        assert resp.default_questions == []

    def test_default_questions_populated(self):
        data = {
            "slug": "x",
            "name": "X",
            "welcome_text": "Hi",
            "followup_enabled": False,
            "followup_count": 3,
            "email_enabled": False,
            "default_questions": [{"category": "Q", "questions": ["One?"]}],
        }
        resp = WorkspaceResponse(**data)
        assert len(resp.default_questions) == 1
        assert resp.default_questions[0].category == "Q"
