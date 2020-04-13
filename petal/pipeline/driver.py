from neo4j import GraphDatabase, basic_auth
import json
import neobolt

from collections import defaultdict
from time import sleep, time
from queue import Empty

from .utils.neo import page, add_json_node
from .module_utils.log import Log
from .module_utils.profile import Profile
from .module_utils.transaction import Transaction
from .batch import Batch

def retry(f):
    def inner(*args, **kwargs):
        waiting = True
        while waiting:
            try:
                return f(*args, **kwargs)
                waiting = False
            except neobolt.exceptions.ServiceUnavailable as e:
                print('Cannot reach neo4j server. Is it running? Sleeping 1s..', flush=True)
                sleep(1)
    return inner

class Driver():
    '''
    An API providing a lightweight connection to neo4j
    '''
    @retry
    def __init__(self, settings_file):
        with open(settings_file, 'r') as infile:
            settings = json.load(infile)
        self.neo_client = GraphDatabase.driver(settings["neo4j_server"], auth=basic_auth(settings["username"], settings["password"]), encrypted=settings["encrypted"])
        self.hset = set()
        self.lset = set()

    @retry
    def run_query(self, query):
        with self.neo_client.session() as session:
            return session.run(query)

    @retry
    def run(self, transaction):
        if transaction.query is not None:
            with self.neo_client.session() as session:
                session.run(transaction.query)
        else:
            id1 = transaction.from_uuid
            id2 = transaction.uuid
            if transaction.data is not None:
                if id2 in self.hset:
                    return False
                self.hset.add(id2)
                self.add(transaction.data, transaction.out_label)
            if id1 is not None and transaction.connect_labels is not None:
                id1 = str(id1)
                key = str(id1) + str(id2)
                if key in self.lset:
                    return False
                self.lset.add(key)
                with self.neo_client.session() as session:
                    session.write_transaction(self.link, id1, id2, transaction.in_label, transaction.out_label, *transaction.connect_labels)
            return True

    def link(self, tx, id1, id2, in_label, out_label, from_label, to_label):
        query = ('MATCH (n:{in_label}) WHERE n.uuid=\'{id1}\' MATCH (m:{out_label}) WHERE m.uuid=\'{id2}\' MERGE (n)-[:{from_label}]->(m) MERGE (m)-[:{to_label}]->(n)'.format(in_label=in_label, out_label=out_label, id1=id1, id2=id2, from_label=from_label, to_label=to_label))
        tx.run(query)

    def add(self, data, label):
        with self.neo_client.session() as session:
            session.write_transaction(add_json_node, label, data)

    @retry
    def get(self, uuid):
        with self.neo_client.session() as session:
            records = list(session.run('MATCH (n) WHERE n.uuid = \'{uuid}\' RETURN n'.format(uuid=str(uuid))).records())
        if len(records) > 0:
            return records[0]['n']
        else:
            raise ValueError('UUID {} invalid'.format(uuid))

    @retry
    def count(self, label):
        with self.neo_client.session() as session:
            records = session.run('MATCH (x:{label}) WITH COUNT (x) AS count RETURN count'.format(label=label)).records()
        return list(records)[0]['count']

def driver_listener(transaction_queue, settings_file):
    profile = Profile('driver')
    log     = Log('driver')

    start = time()
    driver = Driver(settings_file)
    i = 0
    while True:
        batch = transaction_queue.get()
        for transaction in batch.items:
            added = driver.run(transaction)
            if added:
                i += 1
        driver.run(Transaction(out_label='Batch', data={'label' : batch.label, 'filename' : batch.filename, 'rand' : batch.rand}, uuid=batch.uuid))
        duration = time() - start
        total = len(driver.hset) + len(driver.lset)
        log.log('Driver rate: {} of {} ({}|{})'.format(round(total / duration, 3), total, len(driver.hset), len(driver.lset)))
        log.log('Created batch for ', batch.label)