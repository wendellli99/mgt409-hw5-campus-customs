"""Five fully connected PydanticAI agents, bounded shared usage, MCP-only shop tools."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from openai import AsyncOpenAI
from pydantic import Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.messages import TextPart, ToolCallPart
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.toolsets import FilteredToolset
from pydantic_ai.usage import RunUsage, UsageLimits

from audit import append_event, serializable
from models import AgentName, AgentResult

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env', override=False)
load_dotenv(ROOT.parents[1] / '.env', override=False)

MODEL_NAME = 'gpt-6-luna'
ROLES: tuple[AgentName, ...] = ('Boss', 'Inventory', 'Accounting', 'Facilities', 'Customer Service')
PROMPT_FILES = {'Boss': 'boss.md', 'Inventory': 'inventory.md', 'Accounting': 'accounting.md',
                'Facilities': 'facilities.md', 'Customer Service': 'customer_service.md'}
LIMITS = UsageLimits(request_limit=32, total_tokens_limit=75000, tool_calls_limit=60)
MAX_DELEGATION_DEPTH = 3
# Explicit allowlist: human money/reset tools cannot enter any model's tool schema.
SHOP_TOOLS = frozenset({'get_ticket', 'get_inventory', 'get_financial_context', 'list_tickets',
                       'get_shop_snapshot', 'get_vendor_status', 'prepare_payment',
                       'list_approvals', 'draft_message', 'resolve_ticket', 'schedule_restock'})
MUTATING_TOOLS = frozenset({'prepare_payment', 'draft_message', 'resolve_ticket', 'schedule_restock'})


@dataclass
class TeamDeps:
    run_id: str
    ticket_id: int
    agent: AgentName
    path: tuple[AgentName, ...]
    usage: RunUsage
    team: 'AgentTeam'


def usage_dict(usage: RunUsage) -> dict[str, Any]:
    return serializable(usage)


class AgentTeam:
    def __init__(self, model: Any = None):
        if model is None:
            key = os.getenv('PORTKEY_API_KEY', '')
            if not key or key.lower().startswith(('your_', 'replace', 'placeholder')):
                raise RuntimeError('Set PORTKEY_API_KEY in the private .env file before running the agents.')
            client = AsyncOpenAI(api_key=key, base_url='https://api.portkey.ai/v1', timeout=60,
                                 max_retries=0)
            model = OpenAIResponsesModel(MODEL_NAME, provider=OpenAIProvider(openai_client=client))
        self.mcp = MCPServerStreamableHTTP(
            os.getenv('MCP_URL', 'http://127.0.0.1:8001/mcp'), timeout=10,
            read_timeout=20, process_tool_call=self.process_mcp_call,
            allow_sampling=False, include_instructions=False,
        )
        self.shop_tools = FilteredToolset(self.mcp, self.tool_allowed)
        self.agents: dict[AgentName, Agent[TeamDeps, AgentResult]] = {}
        for role in ROLES:
            agent = Agent(model, name=role, deps_type=TeamDeps, output_type=AgentResult,
                          instructions=(ROOT / 'backend' / 'prompts' / PROMPT_FILES[role]).read_text(),
                          toolsets=[self.shop_tools], retries=1, tool_timeout=180,
                          model_settings={'max_tokens': 2200, 'parallel_tool_calls': False,
                                          'openai_reasoning_effort': 'low', 'openai_store': False})
            agent.tool(self.delegate)
            self.agents[role] = agent

    @staticmethod
    def tool_allowed(ctx: RunContext[TeamDeps], definition: Any) -> bool:
        return definition.name in SHOP_TOOLS and (definition.name != 'resolve_ticket' or ctx.deps.agent == 'Boss')

    async def process_mcp_call(self, ctx: RunContext[TeamDeps], call_tool: Any,
                               name: str, args: dict[str, Any]) -> Any:
        deps = ctx.deps
        if name not in SHOP_TOOLS:
            raise ModelRetry('This action is reserved for the human desk operator.')
        if name in MUTATING_TOOLS and args.get('ticket_id') != deps.ticket_id:
            raise ModelRetry('Only change the ticket assigned to this run.')
        append_event(run_id=deps.run_id, ticket_id=deps.ticket_id, agent=deps.agent,
                     event_type='tool_call', message=f'Calling {name}.', tool=name,
                     input=args, delegation_path=deps.path, tokens=usage_dict(deps.usage))
        try:
            async with asyncio.timeout(20):
                result = await call_tool(name, args)
            append_event(run_id=deps.run_id, ticket_id=deps.ticket_id, agent=deps.agent,
                         event_type='tool_result', message=f'{name} returned shop evidence.',
                         tool=name, input=args, output=result, delegation_path=deps.path,
                         tokens=usage_dict(deps.usage))
            return result
        except Exception as exc:
            append_event(run_id=deps.run_id, ticket_id=deps.ticket_id, agent=deps.agent,
                         event_type='tool_error', message=f'{name} failed.', tool=name,
                         input=args, error=type(exc).__name__, delegation_path=deps.path,
                         tokens=usage_dict(deps.usage))
            raise

    async def delegate(self, ctx: RunContext[TeamDeps],
                       agent: AgentName,
                       task: Annotated[str, Field(min_length=1, max_length=2400)]) -> AgentResult:
        """Delegate a focused task to any other teammate; share the same ticket and budget."""
        deps = ctx.deps
        if agent == deps.agent:
            raise ModelRetry('Direct self-delegation is blocked. Choose another teammate or report back.')
        if len(deps.path) - 1 >= MAX_DELEGATION_DEPTH:
            raise ModelRetry('Maximum delegation depth reached. Use existing evidence and report back.')
        if deps.usage.requests >= 31:
            raise ModelRetry('The shared request budget is nearly exhausted. Report the remaining action without another delegation.')
        append_event(run_id=deps.run_id, ticket_id=deps.ticket_id, agent=deps.agent,
                     event_type='delegation', message=f'{deps.agent} asked {agent}: {task}',
                     delegation={'from': deps.agent, 'to': agent, 'task': task},
                     input={'agent': agent, 'task': task}, delegation_path=deps.path,
                     tokens=usage_dict(deps.usage))
        child = TeamDeps(deps.run_id, deps.ticket_id, agent, (*deps.path, agent), deps.usage, self)
        result = await self.run_role(child, task)
        append_event(run_id=deps.run_id, ticket_id=deps.ticket_id, agent=deps.agent,
                     event_type='delegation_result', message=f'{agent} reported: {result.summary}',
                     output=result, delegation={'from': agent, 'to': deps.agent},
                     delegation_path=deps.path, tokens=usage_dict(deps.usage))
        return result

    async def run_role(self, deps: TeamDeps, task: str) -> AgentResult:
        prompt = (f'Assigned ticket: {deps.ticket_id}. Current delegation path: {" → ".join(deps.path)}.\n'
                  f'Task: {task}\nUse MCP for evidence and saved actions. Never approve money. '
                  'If there is a required pending approval, immediately report awaiting_approval=true; '
                  'do not make more tool calls, draft messages, or delegations after that evidence is verified. '
                  'A specialist report based on MCP is sufficient evidence until an actual state change. '
                  'Return a short evidence-based report. Only Boss resolves tickets.')
        append_event(run_id=deps.run_id, ticket_id=deps.ticket_id, agent=deps.agent,
                     event_type='agent_started', message=f'{deps.agent} is investigating ticket {deps.ticket_id}.',
                     input={'task': task}, delegation_path=deps.path, tokens=usage_dict(deps.usage))
        async with self.agents[deps.agent].iter(prompt, deps=deps, usage=deps.usage,
                                              usage_limits=LIMITS) as run:
            async for node in run:
                kind = type(node).__name__
                detail: dict[str, Any] = {'loop_step': kind, 'delegation_path': deps.path,
                                          'tokens': usage_dict(deps.usage)}
                message = f'{deps.agent}: {kind}.'
                if Agent.is_user_prompt_node(node):
                    detail['input'] = {'task': task}
                    message = f'{deps.agent} received its task.'
                elif Agent.is_model_request_node(node):
                    # Model messages contain factual tool outputs, not authentication headers.
                    detail['input'] = serializable(node.request)
                    message = f'{deps.agent} is considering the next step.'
                elif Agent.is_call_tools_node(node):
                    parts = []
                    for part in node.model_response.parts:
                        if isinstance(part, TextPart):
                            parts.append({'text': part.content})
                        elif isinstance(part, ToolCallPart):
                            parts.append({'tool': part.tool_name, 'arguments': part.args_as_dict(),
                                          'tool_call_id': part.tool_call_id})
                    detail['output'] = parts
                    detail['response_usage'] = serializable(node.model_response.usage)
                    spoken = [part['text'] for part in parts if 'text' in part]
                    message = ' '.join(spoken)[:2000] or f'{deps.agent} chose its next tool or report.'
                append_event(run_id=deps.run_id, ticket_id=deps.ticket_id, agent=deps.agent,
                             event_type='loop_step', message=message, **detail)
            if run.result is None:
                raise RuntimeError('The agent finished without a report.')
            result = run.result.output
        append_event(run_id=deps.run_id, ticket_id=deps.ticket_id, agent=deps.agent,
                     event_type='agent_completed', message=result.summary, output=result,
                     delegation_path=deps.path, tokens=usage_dict(deps.usage))
        return result

    async def run_ticket(self, ticket_id: int, run_id: str) -> tuple[AgentResult, RunUsage]:
        usage = RunUsage()
        deps = TeamDeps(run_id, ticket_id, 'Boss', ('Boss',), usage, self)
        async with asyncio.timeout(600):
            result = await self.run_role(deps, 'Investigate and handle this ticket. If resuming after human approval, '
                                               'verify the updated MCP facts and finish the remaining actions. '
                                               'Choose relevant specialists rather than calling everyone.')
        return result, usage
