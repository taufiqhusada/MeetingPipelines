"""Local executor for 'other' agent tasks: Claude + web search finds options and answers.

Darwin finds which outside agent *could* do a task; until Darwin Act is available we run it here instead.
This executor only researches. It never buys, books or messages anyone.
"""
import anthropic

from . import config

SYSTEM = """You are the meeting agent's research executor. A team asked you to handle a task during a meeting.
Do the research part only: find options, facts, prices and links. Never purchase, order, book, sign up for or \
contact anything; if the task asks for that, find the best options and say a human must complete it.

Reply in plain text for a Slack message, under 200 words:
- one line saying what you found
- 2-5 bullets, each with a concrete option or finding, a price or key fact when relevant, and the source link
- one line "Next step for a human: ..." """

MAX_CONTINUATIONS = 5


def run(task: dict) -> dict:
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    prompt = (f"Task from the meeting: {task['title']}\nDetails: {task['details']}\n"
              f"What was said: \"{task['source_quote']}\"")
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}]

    for _ in range(MAX_CONTINUATIONS):
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=4000,
            system=SYSTEM,
            messages=messages,
            tools=tools,
            output_config={"effort": "low"},
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
        if response.stop_reason != "pause_turn":
            break
        # Long server-side search loop paused: resend the turn so the server resumes it.
        messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": response.content}]

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined: {response.stop_details}")
    # Only the text after the last search result is the answer; earlier text is "I'll search..." narration.
    blocks = list(response.content)
    last_result = max((i for i, b in enumerate(blocks) if b.type == "web_search_tool_result"), default=-1)
    text = "".join(b.text for b in blocks[last_result + 1:] if b.type == "text").strip()
    sources = []
    for b in response.content:
        for c in (getattr(b, "citations", None) or []):
            url = getattr(c, "url", None)
            if url and url not in sources:
                sources.append(url)
    if not text:
        raise RuntimeError(f"No answer (stop_reason={response.stop_reason})")
    return {"summary": text, "sources": sources[:8]}
