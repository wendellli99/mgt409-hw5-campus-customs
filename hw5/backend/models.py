"""Typed agent and dashboard contracts. Shop records are supplied only by MCP."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

AgentName = Literal['Boss', 'Inventory', 'Accounting', 'Facilities', 'Customer Service']
RunStatus = Literal['running', 'completed', 'awaiting_approval', 'failed']


class AgentResult(BaseModel):
    summary: str = Field(min_length=1, max_length=6000)
    awaiting_approval: bool = False
    recommended_next_step: str = Field(default='', max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=16)


class Ticket(BaseModel):
    model_config = ConfigDict(extra='allow')
    ticket_id: int
    id: int
    title: str
    description: str
    status: str
    agent_summary: str = ''


class Approval(BaseModel):
    model_config = ConfigDict(extra='allow')
    approval_id: str
    id: str
    ticket_id: int
    kind: str
    amount: float
    payee: str = ''
    description: str
    status: str
    created_at: str


class Cash(BaseModel):
    model_config = ConfigDict(extra='allow')
    balance: float
    starting_balance: float
    total_outflow: float
    account_name: str
    currency: str = 'USD'


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra='allow')
    event_id: str
    run_id: str
    ticket_id: int | None = None
    agent: str
    event_type: str
    message: str
    timestamp: str
    tool: str | None = None
    input: Any = None
    output: Any = None
    tokens: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str
    ticket_id: int
    status: RunStatus = 'running'
    started_at: str
    finished_at: str | None = None
    summary: str = ''
    error: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    contributions: dict[str, str] = Field(default_factory=dict)


class DeskState(BaseModel):
    tickets: list[Ticket]
    cash: Cash
    approvals: list[Approval]
    events: list[AgentEvent]
    runs: list[RunRecord]
    desk_date: str


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    approved_by: str = Field(min_length=1, max_length=80)

    @field_validator('approved_by')
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError('Enter the approving person\'s name.')
        return value
