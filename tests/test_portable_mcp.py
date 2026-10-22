"""Actual MCP SDK v2 protocol checks; only the asynchronous session is mocked."""
import json
import unittest
from unittest.mock import MagicMock, patch
from mcp import Client
from kiroclops.portable.mcp import create_server
from kiroclops.portable.session import Session
from kiroclops.integration.storage import IntegrationError


class PortableMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = MagicMock(spec=Session)
        self.session.inspect.return_value = {"experiment_id": "heart-ground-v1", "candidate_count": 0}
        self.session.submit.side_effect = lambda action, *args: {"job_id": "job-" + action, "status": "queued"}
        self.session.get_job.return_value = {"job_id": "job-evaluate", "status": "running"}
        self.session.cancel_job.return_value = {"status": "cancellation_requested"}
        with patch("kiroclops.portable.mcp.Session", return_value=self.session):
            self.server, _ = create_server("portable-test")

    def payload(self, result):
        self.assertFalse(result.is_error)
        self.assertIsInstance(result.structured_content, dict)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        return result.structured_content

    async def test_exact_seven_names_and_schemas(self):
        async with Client(self.server) as client:
            self.assertEqual(client.server_info.version, "0.3.0")
            tools = {t.name: t for t in (await client.list_tools()).tools}
            self.assertEqual(set(tools), {"inspect_session", "prepare_experiment", "evaluate_candidate", "finalize_candidate", "get_job", "cancel_job", "build_report"})
            self.assertEqual(tools["get_job"].input_schema["properties"]["wait_seconds"]["default"], 10)
            self.assertEqual(tools["get_job"].input_schema["required"], ["job_id"])
            self.assertFalse(tools["finalize_candidate"].input_schema.get("required"))

    async def test_all_calls_use_portable_bound_session(self):
        async with Client(self.server) as client:
            self.assertEqual(self.payload(await client.call_tool("inspect_session", {}))["experiment_id"], "heart-ground-v1")
            for name in ("prepare_experiment", "evaluate_candidate", "finalize_candidate", "build_report"):
                self.assertEqual(self.payload(await client.call_tool(name, {}))["status"], "queued")
            self.assertEqual(self.payload(await client.call_tool("get_job", {"job_id": "job-evaluate"}))["status"], "running")
            self.assertEqual(self.payload(await client.call_tool("cancel_job", {"job_id": "job-evaluate"}))["status"], "cancellation_requested")
        self.session.get_job.assert_called_once_with("job-evaluate", 10)

    async def test_invalid_state_is_explicit_error_and_connection_survives(self):
        self.session.submit.side_effect = IntegrationError("validation_sealed", "Cannot tune a sealed candidate")
        async with Client(self.server) as client:
            result = self.payload(await client.call_tool("evaluate_candidate", {}))
            self.assertEqual(result["error"]["code"], "validation_sealed")
            self.assertEqual(self.payload(await client.call_tool("inspect_session", {}))["candidate_count"], 0)


if __name__ == "__main__":
    unittest.main()
