# PawPal+ AI Agent — Model Card

## Limitations and Biases

PawPal+ uses Google Gemini with function-calling tools to interpret natural-language requests. The model can misunderstand vague time expressions, pet names, task descriptions, priorities, recurrence, or whether a task should be added, changed, or generated. It does not perform independent medical, veterinary, or scheduling-policy judgment and should not be treated as professional advice.

The model can also produce plausible-sounding but incorrect conclusions, select an unexpected tool, or stop before completing the plan–act–check–fix loop. Provider availability, quota limits, latency, model updates, language, and rate limits can change results. The assistant is currently evaluated in English and with a small, local PawPal+ owner/pet/task dataset. Pet names, task language, and demonstration data may therefore be a source of grounding bias.

The scheduling domain remains deterministic: Python functions calculate available slots and detect overlaps, while Gemini decides whether to call tools and how to summarize results. This reduces—but does not eliminate—the risk of incorrect changes.

## Misuse Risks and Prevention

- **Unauthorized schedule changes:** The agent can only operate on the current in-memory owner, but it could still add or reschedule tasks based on ambiguous instructions. The UI and CLI run locally, and tool calls affect only this application's saved `data.json` unless the user explicitly supplies them. We do not provide confirmation before mutation, so the app should be used by someone who reviews the displayed trace and schedule.
- **Unsafe or harmful instructions:** A request could ask for inappropriate pet-care timing or claim a task was completed. The system prompt and domain tools do not execute arbitrary code or external actions, but they do not provide veterinary safeguards. Users must not substitute the app for urgent veterinary care.
- **Prompt injection and fabricated state:** Tool results and natural-language input are separated by the Gemini function-calling interface. The system prompt tells the model to use `list_pets` and not invent pets; unknown pets/tasks are reported through tools rather than silently guessed.
- **API-key and privacy exposure:** API requests require `GOOGLE_API_KEY` or `GEMINI_API_KEY`. Keys must stay in environment variables and must never be committed. Requests sent to Gemini may leave the local machine and are subject to Google's data-handling terms. For sensitive pet or owner data, run only with data the user is comfortable sending to the provider.
- **Repeated or excessive API calls:** The agent loop is bounded to eight iterations. It also returns concise provider errors for authentication, quota, server, and connection failures.

## Testing Surprises

The automated suite **does not call Gemini**. At the latest local run, all 16 tests passed, including agent helper tests for `_find_pet`, `_find_task`, and `_parse_time`.

Live testing produced provider-level surprises:

- A request initially used an unavailable Anthropic-era model name when the implementation was changed; the code was corrected to use Google Gemini.
- A later live request returned a 404-style model-availability error for `gemini-2.5-flash`; the model was updated to `gemini-3.8-flash` based on the provider response.
- Another live request reached Gemini but received HTTP 503 temporary high-demand overload. This showed that key configuration and application error handling can work while hosted-model capacity does not.
- Provider availability therefore remains pending live verification, and “no API key” and “provider temporary overload” are distinct states.

These findings led to clearer provider-specific setup instructions and friendly 401/403, 429, 503, and connection error handling in `run_agent()`.

## Human-AI Collaboration Reflection

While developing this feature, I used AI collaboration to inspect the project, choose an architectural fit, implement tool closures, update the Streamlit and CLI integrations, revise documentation, and run syntax/tests. I reviewed the generated code rather than accepting it wholesale.

**Suggestion accepted:** the plan–act–check–fix structure was changed to use the existing `Scheduler` methods as tools. This was a good fit because it reused deterministic scheduling logic instead of asking a model to calculate every schedule from scratch.

**Suggestion changed/rejected:** the first implementation was designed around the Anthropic SDK and a Claude model. I switched it to Google's `google-genai` SDK because I had a Google API key and wanted the live app to use that credential. I also kept the original unit tests and deterministic PawPal+ workflow instead of replacing existing domain behavior with a standalone AI script.

**What I learned:** a reliable AI feature needs explicit boundaries. Clear tool schemas, real-state lookup, error translation, iteration limits, logging, unit tests, and a manual fallback are more important than making every response look polished. One limitation of my current testing is that it has not yet recorded a complete successful Gemini task-creation-and-conflict-resolution run, so live human evaluation remains pending.

## Responsible Use Summary

Use this project for demonstration and low-stakes schedule assistance. Add or review tasks in the UI, especially when a model moves an existing task. Do not use generated pet-care timings as veterinary advice or in a critical health workflow. Keep API keys private, do not commit secrets, and report provider errors rather than retrying indefinitely.
