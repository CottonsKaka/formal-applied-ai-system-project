"""
PawPal+ AI scheduling assistant — an agentic workflow.

Turns a natural-language request into real changes to an Owner's pets and
tasks by giving Gemini tools that plan (add_task), act (generate_schedule),
check its own work (check_schedule_conflicts), and fix it (find_open_slot +
reschedule_task). Logging and per-tool error handling live inside the tools
themselves so a bad input (an unknown pet, a malformed time) comes back as a
clear message the model can react to, instead of crashing the run.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from dataclasses import dataclass
from datetime import time

from google import genai
from google.genai import errors, types

from pawpal_system import Owner, Pet, Scheduler, Task
from rag import PetCareKnowledgeBase, format_retrieved_context

logger = logging.getLogger(__name__)

MODEL = "gemini-3.8-flash"
MAX_ITERATIONS = 8

SYSTEM_PROMPT = """You are PawPal+'s scheduling assistant. You turn a pet owner's plain-language \
request into real changes to their schedule, using the tools you're given. Work in this loop:

1. PLAN: if the request implies new care tasks, call add_task for each one. If you're not sure a \
pet name the owner used actually exists, call list_pets first — never invent a pet; if it really \
doesn't exist, say so in your final summary instead of guessing.
Use retrieve_context for requests that need pet-care guidance, such as medication, vet visits, feeding, walks, or grooming.
Use retrieved guidance only as general context; it does not replace veterinary advice or prescribed instructions.
2. ACT: call generate_schedule to build today's plan from every task.
3. CHECK: call check_schedule_conflicts to verify the schedule you just built has no overlaps.
4. FIX: if conflicts are reported, use find_open_slot to find room and reschedule_task to move the \
conflicting task there, then call check_schedule_conflicts again. Repeat until it's clean or you've \
made a reasonable number of attempts.
5. Finish with a short, plain-language summary of what you scheduled and any conflicts you fixed.
"""


def _find_pet(owner: Owner, pet_name: str) -> Pet:
    """Find a pet by name (case-insensitive). Raises ValueError listing available pets if not found."""
    for pet in owner.pets:
        if pet.name.lower() == pet_name.lower():
            return pet
    available = ", ".join(p.name for p in owner.pets) or "none"
    raise ValueError(f"No pet named '{pet_name}' found. Available pets: {available}.")


def _find_task(owner: Owner, pet_name: str, description: str) -> Task:
    """Find a task by pet name + description (case-insensitive). Raises ValueError if not found."""
    pet = _find_pet(owner, pet_name)
    for task in pet.tasks:
        if task.description.lower() == description.lower():
            return task
    available = ", ".join(t.description for t in pet.tasks) or "none"
    raise ValueError(f"No task '{description}' found for {pet.name}. Available tasks: {available}.")


def _parse_time(value: str) -> time:
    """Parse an "HH:MM" string into a time. Raises ValueError with a clear message on bad input."""
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"'{value}' isn't a valid time — use 24-hour HH:MM, e.g. '14:30'.") from exc


@dataclass
class AgentResult:
    summary: str
    trace: list[dict]
    schedule: list[Task]


def _log_step(trace: list[dict], tool: str, tool_input: dict, output: str) -> None:
    """Record one tool call for the UI trace and the standard logger alike."""
    trace.append({"tool": tool, "input": tool_input, "output": output})
    logger.info("tool=%s input=%s output=%s", tool, tool_input, output)


def build_tools(owner: Owner, scheduler: Scheduler, trace: list[dict]) -> list:
    """Build the tools for one agent run, closed over this run's owner/scheduler/trace."""

    def list_pets() -> str:
        """List every pet this owner has, by name and species.

        Call this if you're not sure a pet name the owner mentioned actually exists.
        """
        result = "; ".join(f"{p.name} ({p.species})" for p in owner.pets) or "This owner has no pets yet."
        _log_step(trace, "list_pets", {}, result)
        return result

    def add_task(
        pet_name: str,
        description: str,
        task_type: str,
        duration_minutes: int,
        priority: str,
        recurrence: str = "none",
    ) -> str:
        """Add a new care task for one of the owner's pets.

        Args:
            pet_name: The pet's name, exactly as returned by list_pets.
            description: Short description of the task, e.g. "Morning walk".
            task_type: One of "walk", "feeding", "outdoor_activity", "grooming", "medication", "vet", "play".
            duration_minutes: How long the task takes, in minutes.
            priority: One of "low", "medium", "high".
            recurrence: One of "none", "daily", "weekly". Defaults to "none".
        """
        inputs = {
            "pet_name": pet_name,
            "description": description,
            "task_type": task_type,
            "duration_minutes": duration_minutes,
            "priority": priority,
            "recurrence": recurrence,
        }
        try:
            if priority not in ("low", "medium", "high"):
                raise ValueError(f"priority must be 'low', 'medium', or 'high', got '{priority}'.")
            if recurrence not in ("none", "daily", "weekly"):
                raise ValueError(f"recurrence must be 'none', 'daily', or 'weekly', got '{recurrence}'.")
            pet = _find_pet(owner, pet_name)
            pet.add_task(
                Task(
                    pet_id=pet.pet_id,
                    description=description,
                    task_type=task_type,
                    duration_minutes=int(duration_minutes),
                    priority=priority,
                    recurrence=recurrence,
                )
            )
            result = f"Added '{description}' ({duration_minutes} min, {priority} priority) for {pet.name}."
        except Exception as exc:
            result = f"Could not add task: {exc}"
            logger.error("add_task failed: %s", exc)
        _log_step(trace, "add_task", inputs, result)
        return result

    def generate_schedule() -> str:
        """Build today's schedule from every task across all of the owner's pets, ordered by priority then time."""
        try:
            schedule = scheduler.generate_schedule(owner)
            result = scheduler.explain_plan(schedule)
        except Exception as exc:
            result = f"Could not generate schedule: {exc}"
            logger.error("generate_schedule failed: %s", exc)
        _log_step(trace, "generate_schedule", {}, result)
        return result

    def check_schedule_conflicts() -> str:
        """Check the full schedule for overlapping tasks and describe any conflicts found."""
        try:
            conflicts = scheduler.find_conflicts(owner.get_all_tasks())
            result = scheduler.explain_conflicts(owner, conflicts)
        except Exception as exc:
            result = f"Could not check conflicts: {exc}"
            logger.error("check_schedule_conflicts failed: %s", exc)
        _log_step(trace, "check_schedule_conflicts", {}, result)
        return result

    def find_open_slot(duration_minutes: int, after: str = "") -> str:
        """Find the earliest open time slot of at least the given length.

        Args:
            duration_minutes: How long a slot is needed, in minutes.
            after: Optional "HH:MM" time to search after. Leave empty to search from the start of the day.
        """
        inputs = {"duration_minutes": duration_minutes, "after": after}
        try:
            after_time = _parse_time(after) if after else None
            slot = scheduler.find_next_available_slot(
                owner.get_all_tasks(), duration_minutes=int(duration_minutes), after=after_time
            )
            result = slot.strftime("%H:%M") if slot else "No open slot of that length was found today."
        except Exception as exc:
            result = f"Could not find a slot: {exc}"
            logger.error("find_open_slot failed: %s", exc)
        _log_step(trace, "find_open_slot", inputs, result)
        return result

    def reschedule_task(pet_name: str, description: str, new_time: str) -> str:
        """Move an existing task to a new time.

        Args:
            pet_name: The pet's name whose task should be moved.
            description: The task's description, exactly as it was added.
            new_time: The new time in 24-hour "HH:MM" format.
        """
        inputs = {"pet_name": pet_name, "description": description, "new_time": new_time}
        try:
            task = _find_task(owner, pet_name, description)
            task.reschedule(_parse_time(new_time))
            result = f"Moved '{description}' ({pet_name}) to {new_time}."
        except Exception as exc:
            result = f"Could not reschedule: {exc}"
            logger.error("reschedule_task failed: %s", exc)
        _log_step(trace, "reschedule_task", inputs, result)
        return result

    knowledge_base = PetCareKnowledgeBase(Path(__file__).parent / "rag_docs")

    def retrieve_context(query: str) -> str:
        """Retrieve relevant local pet-care guidance for a request."""
        try:
            chunks = knowledge_base.retrieve(query, top_k=3)
            result = format_retrieved_context(chunks)
        except Exception as exc:
            result = f"Could not retrieve pet-care guidance: {exc}"
            logger.error("retrieve_context failed: %s", exc)
        _log_step(trace, "retrieve_context", {"query": query}, result)
        return result

    functions = {
        "list_pets": list_pets,
        "add_task": add_task,
        "generate_schedule": generate_schedule,
        "check_schedule_conflicts": check_schedule_conflicts,
        "find_open_slot": find_open_slot,
        "reschedule_task": reschedule_task,
        "retrieve_context": retrieve_context,
    }

    declarations = [
        types.FunctionDeclaration(
            name="list_pets",
            description="List every pet this owner has, by name and species.",
        ),
        types.FunctionDeclaration(
            name="add_task",
            description="Add a new care task for one of the owner's pets.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "pet_name": types.Schema(type=types.Type.STRING),
                    "description": types.Schema(type=types.Type.STRING),
                    "task_type": types.Schema(type=types.Type.STRING),
                    "duration_minutes": types.Schema(type=types.Type.INTEGER),
                    "priority": types.Schema(type=types.Type.STRING),
                    "recurrence": types.Schema(type=types.Type.STRING),
                },
                required=["pet_name", "description", "task_type", "duration_minutes", "priority"],
            ),
        ),
        types.FunctionDeclaration(
            name="generate_schedule",
            description="Build today's schedule from every task across all of the owner's pets.",
        ),
        types.FunctionDeclaration(
            name="check_schedule_conflicts",
            description="Check the full schedule for overlapping tasks.",
        ),
        types.FunctionDeclaration(
            name="find_open_slot",
            description="Find the earliest open time slot of at least the given duration.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "duration_minutes": types.Schema(type=types.Type.INTEGER),
                    "after": types.Schema(type=types.Type.STRING),
                },
                required=["duration_minutes"],
            ),
        ),
        types.FunctionDeclaration(
            name="retrieve_context",
            description="Retrieve relevant guidance from the local pet-care knowledge base.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"query": types.Schema(type=types.Type.STRING)},
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="reschedule_task",
            description="Move an existing task to a new 24-hour HH:MM time.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "pet_name": types.Schema(type=types.Type.STRING),
                    "description": types.Schema(type=types.Type.STRING),
                    "new_time": types.Schema(type=types.Type.STRING),
                },
                required=["pet_name", "description", "new_time"],
            ),
        ),
    ]

    return [types.Tool(function_declarations=declarations)], functions


def run_agent(
    owner: Owner,
    scheduler: Scheduler,
    user_request: str,
    max_iterations: int = MAX_ITERATIONS,
) -> AgentResult:
    """Run the plan -> act -> check -> fix agentic loop for one natural-language request.

    Raises RuntimeError with a friendly message on missing credentials or Gemini API errors.
    """
    trace: list[dict] = []
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Google API key is missing. Set GOOGLE_API_KEY or GEMINI_API_KEY in your environment."
        )

    try:
        client = genai.Client(api_key=api_key)
        tools, functions = build_tools(owner, scheduler, trace)
        contents = [types.Content(role="user", parts=[types.Part(text=user_request)])]
        final_text = ""

        for _ in range(max_iterations):
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    tools=tools,
                ),
            )

            function_calls = getattr(response, "function_calls", None)
            if not function_calls:
                final_text = response.text or ""
                break

            candidates = getattr(response, "candidates", None)
            if candidates and getattr(candidates[0], "content", None) is not None:
                contents.append(candidates[0].content)

            function_parts = []
            for call in function_calls:
                function = functions.get(call.name)
                if function is None:
                    output = f"Unknown tool: {call.name}"
                else:
                    try:
                        output = function(**dict(call.args or {}))
                    except Exception as exc:
                        output = f"Could not run {call.name}: {exc}"
                        logger.error("%s failed: %s", call.name, exc)

                function_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=call.name,
                            response={"result": output},
                        )
                    )
                )

            contents.append(types.Content(role="user", parts=function_parts))
    except errors.APIError as exc:
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", str(exc))
        if code in (401, 403):
            raise RuntimeError("Google API key is missing or invalid. Check GOOGLE_API_KEY or GEMINI_API_KEY.") from exc
        if code == 429:
            raise RuntimeError("Google API quota or rate limit reached — wait and try again.") from exc
        raise RuntimeError(f"Google Gemini API error ({code or 'unknown'}): {message}") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not run the Gemini assistant: {exc}") from exc

    if not final_text:
        final_text = "The assistant finished without a final summary — check the steps below for what it did."

    return AgentResult(summary=final_text, trace=trace, schedule=scheduler.sort_by_time(owner.get_all_tasks()))
