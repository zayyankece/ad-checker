import os


def client():
    """Anthropic client, or None without a key. gzip only: avoids brotli/zstd version clashes in some envs."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    return anthropic.Anthropic(default_headers={"Accept-Encoding": "gzip"}, max_retries=3)


def text(msg):
    return "\n".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def call_tool(cl, model, system, tool, user, max_tokens=8000):
    """Ask for exactly one tool call. tool_choice stays 'auto' so it also works when the model thinks first."""
    msg = cl.messages.create(
        model=model, max_tokens=max_tokens, system=system + f"\n\nAlways respond by calling the `{tool['name']}` tool exactly once.",
        tools=[tool], messages=[{"role": "user", "content": user}],
    )
    for b in msg.content:
        if getattr(b, "type", "") == "tool_use" and b.name == tool["name"]:
            return b.input
    raise RuntimeError(f"model did not call {tool['name']}; stop_reason={msg.stop_reason}; text={text(msg)[:150]!r}")
