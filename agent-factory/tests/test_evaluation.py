import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml
from opentelemetry.sdk.trace import TracerProvider

from portfolio_agent_factory.evaluation import load_case, run_case, score_case
from portfolio_agent_factory.manifest import Agent


AGENT = Agent("test", "hosted", "invocations", "chat", "handler:run", "suite",
              "Investigate safely", "medium", 3, 2, ("workload_state",))


class EvaluationTests(unittest.TestCase):
    def case(self, **changes):
        data = {
            "schema_version": 1, "id": "ready", "version": 1,
            "incident": {"summary": "Check readiness", "available_tools": ["workload_state"]},
            "tool_fixtures": {"workload_state": {"ready": 0}},
            "reference": {"expected": "Readiness failed"},
            "rubric": {"checks": ["Explain evidence"], "hard_failures": ["Claims repair"],
                       "automated": {"required_tools": ["workload_state"],
                                     "forbidden_answer_patterns": ["(?i)I fixed it"]}},
        }
        data.update(changes)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.yaml"
            path.write_text(yaml.safe_dump(data))
            return load_case(path)

    def test_replay_withholds_grader_and_reports_human_review(self):
        requests = []

        class Responses:
            def create(self, **kwargs):
                requests.append(kwargs)
                if len(requests) == 1:
                    call = SimpleNamespace(type="function_call", name="workload_state", arguments="{}",
                                           call_id="one", caller=None)
                    call.model_dump = lambda exclude_none=True: {"type": "function_call", "name": "workload_state",
                                                                  "arguments": "{}", "call_id": "one"}
                    return SimpleNamespace(status="completed", id="one", output=[call], output_text="")
                return SimpleNamespace(status="completed", id="two", output=[], output_text="Readiness failed")

        result = run_case(self.case(), AGENT, SimpleNamespace(responses=Responses()), "chat",
                          TracerProvider().get_tracer("test"))
        self.assertEqual(result["tool_calls"], ["workload_state"])
        self.assertEqual(result["evaluation"]["status"], "human_review_required")
        self.assertTrue(all(result["evaluation"]["automated_checks"].values()))
        self.assertNotIn("reference", json.dumps(requests))
        self.assertNotIn("rubric", json.dumps(requests))
        self.assertNotIn("hard_failures", json.dumps(requests))

    def test_deterministic_failure_never_claims_semantic_pass(self):
        case = self.case()
        result = {"tool_calls": [], "model_requests": 1, "answer": "I fixed it"}
        report = score_case(case, result, AGENT)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["automated_checks"]["required_tools_used"])
        self.assertFalse(report["automated_checks"]["forbidden_answer_patterns_absent"])
        self.assertEqual(report["human_review"], "pending")

    def test_rejects_leaked_grader_and_oversized_fixture(self):
        with self.assertRaisesRegex(ValueError, "grader fields"):
            self.case(incident={"summary": "x", "reference": "leak",
                                "available_tools": ["workload_state"]})
        with self.assertRaisesRegex(ValueError, "24 KiB cap"):
            self.case(tool_fixtures={"workload_state": {"log": "x" * 24577}})


if __name__ == "__main__":
    unittest.main()
