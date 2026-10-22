"""Small conditional record store; all user keys are derived from verified sub."""
from __future__ import annotations

from copy import deepcopy
import json


class Conflict(Exception):
    pass


class MemoryStore:
    """Deterministic test double with the same compare-and-swap semantics."""
    def __init__(self):
        self.rows = {}

    def get(self, key):
        return deepcopy(self.rows.get(key))

    def list_prefix(self,prefix):
        return [(k,deepcopy(v)) for k,v in sorted(self.rows.items()) if k.startswith(prefix)]

    def transact(self, changes):
        for key, previous, value in changes:
            if self.rows.get(key) != previous:
                raise Conflict()
        for key, previous, value in changes:
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = deepcopy(value)


class DynamoStore:
    def __init__(self, table, client=None):
        import boto3
        self.table = table
        self.client = client or boto3.client('dynamodb')

    @staticmethod
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

    @staticmethod
    def partition(value):
        parts=value.split('#')
        return '#'.join(parts[:2]) if parts[0]=='user' else parts[0]

    @classmethod
    def record_key(cls,value):
        return {'pk':{'S':cls.partition(value)},'sk':{'S':value}}

    def get(self, key):
        item = self.client.get_item(TableName=self.table, Key=self.record_key(key), ConsistentRead=True).get('Item')
        return json.loads(item['value']['S']) if item else None

    def list_prefix(self,prefix):
        token=None
        while True:
            args={'TableName':self.table,'KeyConditionExpression':'pk = :p AND begins_with(sk, :s)',
                  'ExpressionAttributeValues':{':p':{'S':self.partition(prefix)},':s':{'S':prefix}},'ConsistentRead':True}
            if token:args['ExclusiveStartKey']=token
            page=self.client.query(**args)
            for item in page.get('Items',[]):yield item['sk']['S'],json.loads(item['value']['S'])
            token=page.get('LastEvaluatedKey')
            if not token:break

    def transact(self, changes):
        requests = []
        for key, previous, value in changes:
            arguments = {'TableName':self.table, 'ConditionExpression':'attribute_not_exists(pk)' if previous is None else '#v = :old'}
            if previous is not None:
                arguments.update(ExpressionAttributeNames={'#v':'value'}, ExpressionAttributeValues={':old':{'S':self.encoded(previous)}})
            if value is None:
                arguments['Key'] = self.record_key(key)
                requests.append({'Delete':arguments})
            else:
                arguments['Item'] = {**self.record_key(key), 'value':{'S':self.encoded(value)}}
                requests.append({'Put':arguments})
        try:
            self.client.transact_write_items(TransactItems=requests)
        except self.client.exceptions.TransactionCanceledException:
            raise Conflict() from None


def key(tenant, kind, identity=''):
    return 'user#' + tenant + '#' + kind + '#' + identity
