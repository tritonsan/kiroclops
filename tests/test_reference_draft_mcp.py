import tempfile
import unittest
from pathlib import Path

from kiroclops.local_engine.suite_intake import draft
from kiroclops.local_engine.reference_draft import ReferenceDraft
from kiroclops.visual.schema import VisualError

try:
    from mcp import Client
    from kiroclops.local_engine.mcp import create_server
except ImportError:Client=None


class DraftTests(unittest.TestCase):
    def test_missing_long_jump_is_explicit_and_not_an_authoring_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'draft'
            draft(root,'game','Match short and long',['short_jump','long_jump'])
            session=ReferenceDraft(root);value=session.inspect()
            self.assertIn('long_jump',value['reference_coverage']['missing_behaviors'])
            self.assertTrue(any('long key press' in request for request in value['missing_references']))
            self.assertFalse(value['execution_ready']);self.assertFalse(value['model_started'])
            self.assertEqual(session.submit('prepare'),value)
            with self.assertRaises(VisualError):session.submit('evaluate')
            self.assertFalse((root/'session.json').exists())


@unittest.skipIf(Client is None,'MCP SDK is unavailable')
class DraftProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_protocol_keeps_seven_tools_and_reports_missing_media(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'draft';draft(root,'web_panel','Match interrupted panel',['open','close','interrupt','reopen'])
            server,bound=create_server(root,reference_draft=True)
            async with Client(server) as client:
                listed=await client.list_tools()
                self.assertEqual({t.name for t in listed.tools},{'inspect_session','prepare_experiment','evaluate_candidate',
                    'finalize_candidate','get_job','cancel_job','build_report'})
                response=await client.call_tool('prepare_experiment',{})
                self.assertEqual(response.structured_content['status'],'needs_reference')
                self.assertIn('interrupt',response.structured_content['reference_coverage']['missing_behaviors'])
                response=await client.call_tool('evaluate_candidate',{})
                self.assertEqual(response.structured_content['error']['code'],'reference_not_executable')
            bound.close()


if __name__=='__main__':unittest.main()
