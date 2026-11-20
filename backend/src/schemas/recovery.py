from pydantic import BaseModel, Field


class SessionInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class StepInput(BaseModel):
    action: str = Field(min_length=2, max_length=40)
    params: dict = Field(default_factory=dict)
