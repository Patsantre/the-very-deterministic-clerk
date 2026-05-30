import unittest
from types import SimpleNamespace

from ecom_llm_loop import LlmFallbackContext, run_llm_fallback


class ReqRead:
    def __init__(self, path="/proc/demo.json"):
        self.path = path

    def model_dump_json(self):
        return "{}"


class ReportCompletion:
    outcome = "OUTCOME_OK"
    completed_steps_laconic = ["finished"]
    message = "done"
    grounding_refs = []

    def model_dump_json(self):
        return "{}"


class OtherTool:
    def model_dump_json(self):
        return "{}"


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.beta = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(parse=self.parse))
        )

    def parse(self, **_kwargs):
        self.calls += 1
        function = ReqRead() if self.calls == 1 else ReportCompletion()
        job = SimpleNamespace(
            plan_remaining_steps_brief=["next"],
            function=function,
            task_completed=isinstance(function, ReportCompletion),
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=job))]
        )


class LlmLoopTest(unittest.TestCase):
    def test_runtime_error_is_logged_and_not_interpreted_as_tool_success(self):
        client = FakeClient()
        runtime_calls = []

        def call_runtime(cmd):
            runtime_calls.append(cmd)
            if isinstance(cmd, ReqRead):
                raise OSError("runtime timeout")
            return SimpleNamespace()

        ctx = LlmFallbackContext(
            model="test-model",
            client=client,
            task_text="Read a file and finish.",
            log=[],
            call_runtime=call_runtime,
            next_step_schema=object,
            report_completion=ReportCompletion,
            req_inventory_count=OtherTool,
            req_catalogue_count_report=OtherTool,
            req_read=ReqRead,
            req_catalogue_lookup=OtherTool,
            req_store_lookup=OtherTool,
            normalize_store_lookup=lambda cmd, _task: cmd,
            count_policy_request_from_doc=lambda *_args: None,
            format_result=lambda _cmd, _result: "ok",
        )

        run_llm_fallback(ctx)

        self.assertEqual(client.calls, 2)
        self.assertEqual(len(runtime_calls), 2)
        self.assertTrue(
            any(
                item.get("tool_call_id") == "step_1"
                and "runtime timeout" in item.get("content", "")
                for item in ctx.log
            )
        )


if __name__ == "__main__":
    unittest.main()
