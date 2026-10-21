from sqlalchemy import Column, String, Integer, Boolean, JSON
from db import Base


class Workspace(Base):
    __tablename__ = "workspaces"

    # The slug used in the URL, e.g. "admissions"
    slug = Column(String, primary_key=True, index=True)

    # Human-readable display name, e.g. "Admissions Office"
    name = Column(String, nullable=False, default="")

    # Empty-state placeholder shown in the chat window before the first message
    welcome_text = Column(String, nullable=False, default="Send a message to get started.")

    # Follow-up questions feature
    followup_enabled = Column(Boolean, nullable=False, default=False)
    followup_count = Column(Integer, nullable=False, default=3)

    # Email skill — when enabled, the email prompt suffix is injected into every request
    email_enabled = Column(Boolean, nullable=False, default=False)

    # Default questions: stored as JSON list of {category, questions[]} objects
    # e.g. [{"category": "Getting Started", "questions": ["What can you help me with?"]}]
    default_questions = Column(JSON, nullable=False, default=list)
