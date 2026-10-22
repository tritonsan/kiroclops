"""Read-only MCP preparation view of incomplete multi-reference intake.

The draft cannot run, edit or score a candidate. A frozen pixel measurement
suite and an executable local session are separate subsequent steps.
"""
from pathlib import Path
from kiroclops.visual.schema import VisualError, read_json
from .suite_intake import coverage


class ReferenceDraft:
    def __init__(self,directory):
        self.directory=Path(directory).resolve()
        self.inspect()

    def inspect(self):
        status=coverage(self.directory)
        draft=read_json(self.directory/'draft.json')
        return {'kind':'MultiReferencePreparation','domain':draft['domain'],'goal':draft['goal'],
            'status':'needs_reference' if status['missing_behaviors'] else 'ready_for_adapter_binding','reference_coverage':status,'editable_scope':[],
            'missing_references':status['requests'],'execution_ready':False,
            'heldout_status':'not_provided','tools_write_motion_code':False,
            'model_started':False,'message':'Add the missing recordings on the same local screen; the measurement suite and the editing session have not been opened yet.'}

    def submit(self,operation,candidate_id=None):
        if operation=='prepare':return self.inspect()
        if operation=='report':return self.inspect()
        raise VisualError('reference_not_executable','Complete reference collection and bind an executable session before authoring candidates.')

    def get_job_feedback(self,*args):raise VisualError('job_not_found','Reference collection has no application execution job.')
    def cancel_job(self,*args):raise VisualError('job_not_found','Reference collection has no owned application process.')
    def close(self):pass
