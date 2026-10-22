"""Service v2: immutable measurements, execution receipts, optional advice."""
from __future__ import annotations

from copy import deepcopy
from kiroclops.visual.schema import VisualError, digest
from .identity import computation_key


def request_binding(package_sha256, selection, contract, candidate_sha256=None):
    return {'operation': 'analyze', 'package_sha256': package_sha256, 'selection': deepcopy(selection),
            'contract': deepcopy(contract), 'candidate_sha256': candidate_sha256}


def make_measurement(observations, events):
    if not observations.get('package_sha256'):
        raise VisualError('result_binding_mismatch', 'Observation package binding is required')
    return {'schema_version': 1, 'kind': 'ImmutableMeasurement',
            'package_sha256': observations['package_sha256'],
            'measurement_status': observations.get('status', 'ok'),
            'observations': deepcopy(observations), 'events': deepcopy(events)}


def envelope(measurement, request, *, logical_job_id, engine, sources, timings, origin=None):
    if measurement['package_sha256'] != request['package_sha256']:
        raise VisualError('result_binding_mismatch', 'Measurement belongs to another package')
    key = computation_key(request['package_sha256'], request['selection'], request.get('contract'), sources, engine)
    receipt = {'schema_version': 1, 'logical_job_id': logical_job_id, 'request_sha256': digest(request),
               'package_sha256': request['package_sha256'], 'candidate_sha256': request.get('candidate_sha256'),
               'computation_key': key, 'analysis_sources': deepcopy(sources),
               'engine': {'identity': deepcopy(engine['identity']), 'engine_sha256': engine['engine_sha256']},
               'timings': deepcopy(timings), 'origin': deepcopy(origin or {'kind': 'fresh'}),
               'measurement_sha256': digest(measurement)}
    return {'schema_version': 2, 'kind': 'AnalysisDelivery', 'measurement': deepcopy(measurement),
            'measurement_sha256': digest(measurement), 'receipt': receipt,
            'receipt_sha256': digest(receipt), 'advice': None, 'indexed_for_similarity': False}


def validate_delivery(value, request, *, logical_job_id, expected_engine_sha256=None, expected_sources=None):
    try:
        if value['schema_version'] != 2 or value['kind'] != 'AnalysisDelivery':
            raise ValueError('Unsupported delivery schema')
        measurement, receipt = value['measurement'], value['receipt']
        if value['receipt_sha256'] != digest(receipt) or value['measurement_sha256'] != digest(measurement):
            raise ValueError('Receipt or measurement checksum differs')
        if (receipt['measurement_sha256'] != value['measurement_sha256']
                or receipt['logical_job_id'] != logical_job_id or receipt['request_sha256'] != digest(request)
                or receipt['candidate_sha256'] != request.get('candidate_sha256')
                or receipt['package_sha256'] != request['package_sha256']
                or measurement['package_sha256'] != request['package_sha256']
                or measurement['observations']['package_sha256'] != request['package_sha256']):
            raise ValueError('Delivery belongs to another job, candidate or input')
        key = computation_key(request['package_sha256'], request['selection'], request.get('contract'),
                              receipt['analysis_sources'], receipt['engine'])
        if receipt['computation_key'] != key:
            raise ValueError('Computation identity differs')
        if expected_engine_sha256 is not None and receipt['engine']['engine_sha256'] != expected_engine_sha256:
            raise ValueError('Engine differs from the selected service version')
        if expected_sources is not None and receipt['analysis_sources'] != expected_sources:
            raise ValueError('Analysis sources differ from the selected service version')
    except (KeyError, TypeError, ValueError) as exc:
        raise VisualError('result_binding_mismatch', str(exc)) from exc
    return value


def legacy_result(delivery, request, *, logical_job_id, **validation):
    validate_delivery(delivery, request, logical_job_id=logical_job_id, **validation)
    measurement = delivery['measurement']
    return {'schema_version': 1, 'kind': 'AnalysisResult', 'status': measurement['measurement_status'],
            'measurement_status': measurement['measurement_status'], 'package_sha256': request['package_sha256'],
            'candidate_sha256': request.get('candidate_sha256'), 'request_sha256': digest(request),
            'observations': deepcopy(measurement['observations']), 'events': deepcopy(measurement['events'])}
