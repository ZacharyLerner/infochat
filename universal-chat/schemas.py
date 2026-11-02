from pydantic import BaseModel, ConfigDict, Field

_EXAMPLE_CATEGORIES = [
    {
        "category": "Getting Started",
        "questions": [
            "What can you help me with?",
            "Give me a summary of what you know.",
        ],
    },
    {
        "category": "Policies",
        "questions": [
            "What is the refund policy?",
            "How do I contact support?",
        ],
    },
]


class CannedQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=120, description="Chip text, sent as the user's message.")
    answer: str = Field(min_length=1, max_length=4000, description="Markdown reply shown instead of calling the LLM.")


class DefaultQuestionCategory(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "category": "Getting Started",
                "questions": [
                    "What can you help me with?",
                    "Give me a summary of what you know.",
                ],
            }
        }
    )

    category: str = Field(min_length=1, max_length=40, description="Category label shown above the question chips.")
    questions: list[str | CannedQuestion] = Field(
        default=[],
        description="Clickable questions: a plain string asks the LLM, an object gives a canned answer.",
    )


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "slug": "admissions",
                "name": "Admissions Office",
                "followup_enabled": True,
                "followup_count": 3,
                "default_questions": _EXAMPLE_CATEGORIES,
            }
        }
    )

    slug: str = Field(min_length=1, max_length=100, description="URL slug for the workspace, e.g. 'admissions'.")
    name: str = Field(min_length=1, max_length=200, description="Human-readable display name, e.g. 'Admissions Office'.")
    welcome_text: str = Field(default="Send a message to get started.", max_length=300, description="Empty-state text shown in the chat window before any messages.")
    followup_enabled: bool = Field(default=False, description="When true, the LLM is prompted to append follow-up question suggestions to each response.")
    followup_count: int = Field(default=3, ge=1, le=5, description="Number of follow-up questions to generate (1–5).")
    email_enabled: bool = Field(default=False, description="When true, the email skill prompt is injected into every request, allowing users to send emails via the chat.")
    canned_delay_seconds: float = Field(default=1.0, ge=0, le=30, description="Seconds the typing indicator shows before a canned answer.")
    default_questions: list[DefaultQuestionCategory] = Field(
        default=[],
        description="Categories of preset questions shown above the chat input. Each category has a label and a list of question strings.",
    )


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Admissions Office",
                "followup_enabled": True,
                "followup_count": 3,
                "default_questions": _EXAMPLE_CATEGORIES,
            }
        }
    )

    name: str = Field(min_length=1, max_length=200, description="Human-readable display name.")
    welcome_text: str = Field(default="Send a message to get started.", max_length=300, description="Empty-state text shown in the chat window before any messages.")
    followup_enabled: bool = Field(default=False, description="When true, the LLM is prompted to append follow-up question suggestions to each response.")
    followup_count: int = Field(default=3, ge=1, le=5, description="Number of follow-up questions to generate (1–5).")
    email_enabled: bool = Field(default=False, description="When true, the email skill prompt is injected into every request.")
    canned_delay_seconds: float = Field(default=1.0, ge=0, le=30, description="Seconds the typing indicator shows before a canned answer.")
    default_questions: list[DefaultQuestionCategory] = Field(
        default=[],
        description="Categories of preset questions shown above the chat input.",
    )


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "slug": "admissions",
                "name": "Admissions Office",
                "followup_enabled": True,
                "followup_count": 3,
                "default_questions": _EXAMPLE_CATEGORIES,
            }
        },
    )

    slug: str
    name: str
    welcome_text: str
    followup_enabled: bool
    followup_count: int
    email_enabled: bool
    canned_delay_seconds: float = 1.0
    default_questions: list[DefaultQuestionCategory] = []
