"""Real SDK v2 in-memory protocol tests; Session is the only mocked boundary.

Run with .venv-kiro/Scripts/python.exe. No captures, model calls or child
processes are started. The Phase 1-only environment may omit the MCP SDK.
"""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, call, patch

try:
    from mcp import Client
except ImportError:
    Client = None

if Client is not None:
    from kiroclops.integration.mcp import create_server
    from kiroclops.integration.session import Session
    from kiroclops.integration.storage import IntegrationError


@unittest.skipIf(Client is None, "MCP SDK v2 is installed in .venv-kiro, not the Phase 1 environment")
class InMemoryMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = MagicMock(spec=Session)
        self.session.inspect.return_value = {"session_id": "protocol-test", "status": "ready", "candidate_count": 0}
        self.session.submit.side_effect = lambda action, *args: {"job_id": f"job-{action}", "status": "queued", "candidate_id": args[0] if args else None}
        self.session.get_job.return_value = {"job_id": "job-evaluate", "status": "running", "progress": "capture pending"}
        self.session.cancel_job.return_value = {"job_id": "job-evaluate", "status": "cancellation_requested"}
        with patch("kiroclops.integration.mcp.Session", return_value=self.session) as constructor:
            self.server, self.bound = create_server(Path("mock-session"))
        constructor.assert_called_once_with(Path("mock-session"))
        self.assertIs(self.bound, self.session)

    def payload(self, result):
        self.assertFalse(result.is_error)
        self.assertIsInstance(result.structured_content, dict, "Tools must supply real MCP structured_content, not just JSON text")
        # SDK sends both forms; keep them consistent for clients of either form.
        text = [block.text for block in result.content if block.type == "text"]
        self.assertEqual(len(text), 1)
        self.assertEqual(json.loads(text[0]), result.structured_content)
        return result.structured_content

    async def test_handshake_and_all_seven_tool_schemas(self):
        async with Client(self.server) as client:
            self.assertEqual(client.server_info.name, "kiroclops")
            self.assertEqual(client.server_info.version, "0.2.0")
            self.assertTrue(client.protocol_version)
            self.assertIn("No tool writes motion code", client.instructions)
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            self.assertEqual(set(tools), {"inspect_session", "prepare_experiment", "evaluate_candidate",
                                         "finalize_candidate", "get_job", "cancel_job", "build_report"})
            for name, tool in tools.items():
                with self.subTest(tool=name):
                    self.assertEqual(tool.input_schema["type"], "object")
                    self.assertEqual(tool.output_schema["type"], "object")
                    self.assertTrue(tool.description)
            for name in ("inspect_session", "prepare_experiment", "evaluate_candidate", "build_report"):
                self.assertEqual(tools[name].input_schema["properties"], {})
                self.assertFalse(tools[name].input_schema.get("required"))
            final = tools["finalize_candidate"].input_schema
            self.assertFalse(final.get("required"))
            self.assertEqual({arm["type"] for arm in final["properties"]["candidate_id"]["anyOf"]}, {"string", "null"})
            job = tools["get_job"].input_schema
            self.assertEqual(job["required"], ["job_id"])
            self.assertEqual(job["properties"]["job_id"]["type"], "string")
            self.assertEqual(job["properties"]["wait_seconds"]["default"], 10)
            self.assertEqual(job["properties"]["wait_seconds"]["type"], "integer")
            self.assertEqual(tools["cancel_job"].input_schema["required"], ["job_id"])
        self.session.inspect.assert_not_called()
        self.session.submit.assert_not_called()

    async def test_all_tools_dispatch_arguments_and_preserve_structured_results(self):
        async with Client(self.server) as client:
            inspected = self.payload(await client.call_tool("inspect_session", {}))
            self.assertEqual(inspected["session_id"], "protocol-test")
            for name, action in (("prepare_experiment", "prepare"), ("evaluate_candidate", "evaluate"), ("build_report", "report")):
                result = self.payload(await client.call_tool(name, {}))
                self.assertEqual(result["job_id"], f"job-{action}")
                self.assertEqual(result["status"], "queued")
            chosen = self.payload(await client.call_tool("finalize_candidate", {"candidate_id": "candidate-02"}))
            self.assertEqual(chosen["candidate_id"], "candidate-02")
            default = self.payload(await client.call_tool("finalize_candidate", {}))
            self.assertIsNone(default["candidate_id"])
            self.payload(await client.call_tool("get_job", {"job_id": "job-evaluate", "wait_seconds": 0}))
            self.payload(await client.call_tool("get_job", {"job_id": "job-evaluate"}))
            cancelled = self.payload(await client.call_tool("cancel_job", {"job_id": "job-evaluate"}))
            self.assertEqual(cancelled["status"], "cancellation_requested")
        self.session.inspect.assert_called_once_with()
        self.assertEqual(self.session.submit.call_args_list, [call("prepare"), call("evaluate"), call("report"),
                                                             call("finalize", "candidate-02"), call("finalize", None)])
        self.assertEqual(self.session.get_job.call_args_list, [call("job-evaluate", 0), call("job-evaluate", 10)])
        self.session.cancel_job.assert_called_once_with("job-evaluate")

    async def test_queued_work_returns_without_polling_or_waiting_for_completion(self):
        async with Client(self.server) as client:
            # This unfinished future represents the worker lifecycle, deliberately
            # unrelated to the immediate submission result.
            worker_finished = asyncio.get_running_loop().create_future()
            try:
                result = self.payload(await asyncio.wait_for(client.call_tool("evaluate_candidate", {}), timeout=3))
                self.assertEqual(result["status"], "queued")
                self.assertFalse(worker_finished.done())
                self.session.get_job.assert_not_called()
                self.payload(await asyncio.wait_for(client.call_tool("inspect_session", {}), timeout=3))
                running = self.payload(await client.call_tool("get_job", {"job_id": "job-evaluate", "wait_seconds": 0}))
                self.assertEqual(running["status"], "running")
                self.assertFalse(worker_finished.done())
            finally:
                worker_finished.cancel()

    async def test_known_failures_have_explicit_machine_codes_and_leave_connection_usable(self):
        async with Client(self.server) as client:
            for error, code in ((IntegrationError("budget_exhausted", "Three attempts used"), "budget_exhausted"),
                                (ValueError("bad candidate"), "invalid_request"),
                                (OSError("evidence unavailable"), "invalid_request")):
                with self.subTest(code=code, error=type(error).__name__):
                    self.session.submit.side_effect = error
                    # Domain errors intentionally have is_error=False and an
                    # explicit status/error payload; clients must inspect status.
                    result = self.payload(await client.call_tool("evaluate_candidate", {}))
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"]["code"], code)
                    self.assertEqual(result["error"]["message"], str(error))
            healthy = self.payload(await client.call_tool("inspect_session", {}))
            self.assertEqual(healthy["status"], "ready")

    async def test_missing_and_wrong_arguments_are_rejected_before_session_dispatch(self):
        async with Client(self.server) as client:
            for name, arguments in (("get_job", {}), ("cancel_job", {}),
                                    ("get_job", {"job_id": "j1", "wait_seconds": "not-an-integer"}),
                                    ("finalize_candidate", {"candidate_id": {"bad": "object"}})):
                with self.subTest(tool=name, arguments=arguments):
                    result = await client.call_tool(name, arguments)
                    self.assertTrue(result.is_error)
                    self.assertTrue(result.content)
            self.session.get_job.assert_not_called()
            self.session.cancel_job.assert_not_called()
            self.session.submit.assert_not_called()
            healthy = self.payload(await client.call_tool("inspect_session", {}))
            self.assertEqual(healthy["status"], "ready")

    async def test_unknown_tool_cannot_dispatch_arbitrary_session_methods(self):
        async with Client(self.server) as client:
            result = await client.call_tool("close", {})
            self.assertTrue(result.is_error)
            self.session.close.assert_not_called()
            self.session.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
