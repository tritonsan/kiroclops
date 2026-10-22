import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kiroclops.cloud.execution import backend_for_store
from kiroclops.cloud.results import request_binding
from kiroclops.local_engine.intake import _qualify_automatic
from kiroclops.visual.schema import VisualError,write_json,read_json


class RoutingTests(unittest.TestCase):
    def test_kiro_mcp_environment_pins_explicit_remote_config_without_tokens(self):
        from kiroclops.local_engine.session import create_session
        from kiroclops.local_engine.launcher import configure,load_config
        with tempfile.TemporaryDirectory(prefix='kiro cloud ') as temporary:
            root=Path(temporary);source=root/'source';(source/'src').mkdir(parents=True)
            (source/'src/panel-motion.js').write_text('export const ready = true;')
            reference=root/'reference';reference.mkdir()
            config_path=root/'service.json';write_json(config_path,{'api_url':'https://service.example','capability_file':'outside-private-file'})
            session=root/'session'
            create_session(session,'web_panel',source,reference,'Transfer motion',mode='tools')
            with patch.dict(os.environ,{'KIROCLOPS_ANALYSIS_BACKEND':'remote','KIROCLOPS_CLOUD_CONFIG':str(config_path)}):
                result=configure(session)
            server=read_json(result['agent_path'])['mcpServers']['kiroclops']
            self.assertEqual(server['env']['KIROCLOPS_ANALYSIS_BACKEND'],'remote')
            self.assertEqual(server['env']['KIROCLOPS_CLOUD_CONFIG'],str(config_path.resolve()))
            self.assertNotIn('access_token',str(server))
            load_config(session)
            write_json(config_path,{'changed':True})
            with self.assertRaises(VisualError) as error:load_config(session)
            self.assertEqual(error.exception.code,'config_changed')

    def test_unconfigured_remote_cannot_silently_use_local_cli_store(self):
        with patch.dict(os.environ,{'KIROCLOPS_ANALYSIS_BACKEND':'remote'},clear=True):
            with self.assertRaises(VisualError) as error:backend_for_store('unused')
            self.assertEqual(error.exception.code,'remote_not_configured')

    def test_request_binding_snapshots_nested_values(self):
        selection={'objects':[{'id':'one'}]};contract={'options':{'x':1}}
        bound=request_binding('hash',selection,contract)
        selection['objects'][0]['id']='two';contract['options']['x']=2
        self.assertEqual(bound['selection']['objects'][0]['id'],'one')
        self.assertEqual(bound['contract']['options']['x'],1)

    def test_intake_preserves_operational_failure_and_only_selection_errors_prompt(self):
        proposals={'package_sha256':'hash','proposals':[{'basis':'visible_outline','activity_pixels':15,'roi':[10,10,20,20]},
                    {'basis':'motion_envelope','roi':[10,10,20,20]}]}
        with patch('kiroclops.local_engine.intake._background_regions',return_value=[[1,1,5,5]]):
            with patch('kiroclops.cloud.execution.analyze_package',side_effect=VisualError('analysis_connection_lost','Reconnect')):
                with self.assertRaises(VisualError) as error:_qualify_automatic('package',proposals)
                self.assertEqual(error.exception.code,'analysis_connection_lost')
            with patch('kiroclops.cloud.execution.analyze_package',side_effect=VisualError('invalid_selection','Select')):
                selection,status=_qualify_automatic('package',proposals)
                self.assertIsNone(selection);self.assertEqual(status['status'],'needs_confirmation')
