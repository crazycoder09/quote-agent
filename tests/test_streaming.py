"""Live answer streaming in the chat UI (main.LiveAnswer) - no AI calls."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class FakeSlot:
    def __init__(self):
        self.shown, self.cleared = [], 0

    def markdown(self, text):
        self.shown.append(text)

    def empty(self):
        self.cleared += 1


class LiveAnswerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main  # noqa: F401 - Streamlit runs "bare" outside the app, which is fine for this class

        cls.LiveAnswer = main.LiveAnswer

    def chunk(self, text="", id_="run1", tool=False):
        from langchain_core.messages import AIMessageChunk

        kw = {"tool_call_chunks": [{"name": "find_products", "args": "{}", "id": "t1", "index": 0}]} if tool else {}
        return AIMessageChunk(content=text, id=id_, **kw)

    def feed(self, live, *chunks, node="agent"):
        for c in chunks:
            live.last = 0.0  # no throttling in the test
            live.feed(c, {"langgraph_node": node})

    def test_streams_agent_text_only(self):
        slot = FakeSlot()
        live = self.LiveAnswer(slot)
        self.feed(live, self.chunk('{"intent": "work"'), node="understand")  # router output: never shown
        self.assertEqual(slot.shown, [])
        self.feed(live, self.chunk("Your quote "), self.chunk("is $271.92"))
        self.assertTrue(slot.shown[-1].startswith("Your quote is \\$271.92"))  # $ escaped for Streamlit

    def test_tool_step_text_cleared_and_new_model_restarts(self):
        slot = FakeSlot()
        live = self.LiveAnswer(slot)
        self.feed(live, self.chunk("Let me look that up", id_="a"), self.chunk("", id_="a", tool=True))
        self.assertGreaterEqual(slot.cleared, 1)
        self.feed(live, self.chunk("Partial answer from model 1", id_="b"))
        self.feed(live, self.chunk("Final", id_="c"), self.chunk(" answer", id_="c"))  # next model answers
        self.assertEqual(live.text, "Final answer")

    def test_claude_content_blocks(self):
        from langchain_core.messages import AIMessageChunk

        slot = FakeSlot()
        live = self.LiveAnswer(slot)
        live.feed(AIMessageChunk(content=[{"type": "text", "text": "Hi there", "index": 0}], id="x"), {"langgraph_node": "agent"})
        self.assertEqual(live.text, "Hi there")


if __name__ == "__main__":
    unittest.main()
