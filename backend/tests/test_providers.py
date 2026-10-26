"""Real-provider code paths, exercised with stub SDK clients (no network, no keys)."""
import copy
import json
from types import SimpleNamespace as NS

from app.agents.schemas import EvidenceAnswer, Extraction
from app.llm.base import Tool
from app.llm.prompts import load_prompt
from app.llm.providers import AzureOpenAIProvider, ClaudeFoundryProvider

GOOD = {"order_lines": [{"sku": "SKU-1", "qty": 2}], "commitment_type": "standard", "requested_date": None,
        "delivery_address": {"line1": "1 A Street", "city": "X", "state": "OH", "postcode": "43001"},
        "missing_fields": [], "evidence": {}, "confidence": 0.9}


class Block(NS):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


class StubAnthropic:
    def __init__(self, responses, reject_temperature=False):
        self.responses, self.calls, self.reject = list(responses), [], reject_temperature
        self.messages = self

    def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        if self.reject and "temperature" in kw:
            raise RuntimeError("temperature is not supported for this model")
        return self.responses.pop(0)


def _tool_use(name, inp, id_="t1"):
    return NS(content=[Block(type="tool_use", id=id_, name=name, input=inp)],
              usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=80))


def test_claude_structured_forces_tool_caches_prompt_and_drops_temperature():
    stub = StubAnthropic([_tool_use("record_output", GOOD)], reject_temperature=True)
    out = ClaudeFoundryProvider(client=stub).structured(load_prompt("p1_email_extraction"), "hi", Extraction)
    assert out.order_lines[0].sku == "SKU-1"
    final = stub.calls[-1]
    assert final["tool_choice"] == {"type": "tool", "name": "record_output"}
    assert final["system"][0]["cache_control"] == {"type": "ephemeral"} and "temperature" not in final


def test_claude_structured_repairs_invalid_output_once():
    bad = dict(GOOD, confidence=7)
    stub = StubAnthropic([_tool_use("record_output", bad), _tool_use("record_output", GOOD, "t2")])
    out = ClaudeFoundryProvider(client=stub).structured(load_prompt("p1_email_extraction"), "hi", Extraction)
    assert out.confidence == 0.9 and stub.calls[1]["messages"][-1]["content"][0]["is_error"]


def test_claude_tool_loop_runs_tools_then_final_answer():
    stub = StubAnthropic([_tool_use("run_sql", {"query": "select 1"}),
                          _tool_use("final_answer", {"answer": "1", "sql": "select 1", "figures": [], "answerable": True}, "t2")])
    tool = Tool("run_sql", "q", {"type": "object"}, lambda a: {"rows": [[1]], "columns": ["x"]})
    ans, trace = ClaudeFoundryProvider(client=stub).tool_loop(load_prompt("p4_evidence_agent"), "q", [tool], EvidenceAnswer)
    assert ans.answer == "1" and trace[0]["output"]["rows"] == [[1]]
    assert stub.calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"


class StubOpenAI:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.chat = NS(completions=self)

    def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        return self.responses.pop(0)


def _oa(name, args, id_="c1"):
    call = NS(id=id_, function=NS(name=name, arguments=json.dumps(args)))
    return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))],
              usage=NS(prompt_tokens=50, completion_tokens=10, prompt_tokens_details=NS(cached_tokens=0)))


def test_azure_openai_structured_and_tool_loop():
    stub = StubOpenAI([_oa("record_output", GOOD)])
    p = AzureOpenAIProvider(client=stub)
    assert p.structured(load_prompt("p1_email_extraction"), "hi", Extraction).order_lines[0].qty == 2
    assert stub.calls[0]["tool_choice"]["function"]["name"] == "record_output"
    stub2 = StubOpenAI([_oa("run_sql", {"query": "select 1"}),
                        _oa("final_answer", {"answer": "ok", "figures": [], "answerable": True}, "c2")])
    tool = Tool("run_sql", "q", {"type": "object"}, lambda a: {"rows": [[1]]})
    ans, trace = AzureOpenAIProvider(client=stub2).tool_loop(load_prompt("p4_evidence_agent"), "q", [tool], EvidenceAnswer)
    assert ans.answer == "ok" and stub2.calls[1]["messages"][-1]["role"] == "tool"


def test_markup_leak_in_final_answer_triggers_repair():
    leaked = {"answer": "Carrier D is worst.</answer>\n<parameter name=\"sql\">SELECT 1", "answerable": True, "figures": []}
    clean = {"answer": "Carrier D is worst at 39.5% late.", "sql": "SELECT 1", "figures": [], "answerable": True}
    stub = StubAnthropic([_tool_use("final_answer", leaked), _tool_use("final_answer", clean, "t2")])
    ans, _ = ClaudeFoundryProvider(client=stub).tool_loop(load_prompt("p4_evidence_agent"), "q", [], EvidenceAnswer)
    assert "<" not in ans.answer and ans.sql == "SELECT 1"
    assert stub.calls[1]["messages"][-1]["content"][0]["is_error"]  # model was asked to fix its call


def test_thinking_model_falls_back_to_auto_tool_choice_and_nudges():
    class ThinkingStub(StubAnthropic):
        def create(self, **kw):
            if kw.get("tool_choice", {}).get("type") in ("tool", "any"):
                self.calls.append(copy.deepcopy(kw))
                raise RuntimeError("tool_choice forces tool use, which is not compatible with thinking enabled")
            return super().create(**kw)

    text_only = NS(content=[Block(type="thinking", thinking="hmm", signature="s"), Block(type="text", text="Here it is")],
                   usage=NS(input_tokens=5, output_tokens=5, cache_read_input_tokens=0))
    stub = ThinkingStub([text_only, _tool_use("record_output", GOOD)], reject_temperature=True)
    p = ClaudeFoundryProvider(client=stub)
    out = p.structured(load_prompt("p1_email_extraction"), "hi", Extraction)
    assert out.order_lines[0].sku == "SKU-1"
    assert stub.calls[-1]["tool_choice"] == {"type": "auto"} and "temperature" not in stub.calls[-1]
    assert "record_output" in stub.calls[-1]["messages"][-1]["content"]  # nudged after a text-only reply
    assert p._no_forced_tools and p._no_temperature  # learned once, reused for later calls
