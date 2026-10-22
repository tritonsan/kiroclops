"""Binding repairs preserve the recorded timeline and its certainty."""
from copy import deepcopy
import unittest
from scripts.compare_svg_recorder_qualification import binding_for


class SVGRecorderReplayTests(unittest.TestCase):
    def test_binding_uses_actual_provenance_without_mutating_record(self):
        for provenance, claim in [('recorded', 'recorded_input_response'),
                                  ('user_declared', 'conditional_on_user_declared_inputs')]:
            record={'observations': {'inputs': {'provenance': provenance}},
                    'selection': {'lifecycle_bindings': {'a': [0, 1]}, 'connections': []}}
            scenario={'domain':'svg', 'events':[{'time_s':.4,'action':'click'}]}
            original=deepcopy(record),deepcopy(scenario)
            binding=binding_for(record,scenario)
            self.assertEqual(provenance,binding['input_provenance'])
            self.assertEqual(claim,binding['claim'])
            self.assertEqual(original,(record,scenario))
            binding['scenario']['events'][0]['time_s']=100
            self.assertEqual(.4,scenario['events'][0]['time_s'])

    def test_unknown_inputs_never_gain_recorded_certainty(self):
        with self.assertRaisesRegex(ValueError,'known input'):
            binding_for({'observations':{'inputs':{'provenance':'unknown'}}},{})
