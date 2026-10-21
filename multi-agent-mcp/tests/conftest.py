"""Tests run offline: the SDK's trace export would try to reach OpenAI and log that it has no key."""

from agents import set_tracing_disabled

set_tracing_disabled(True)
