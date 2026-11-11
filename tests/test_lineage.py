"""Tenant-local lineage is read-only, bounded and never a recall command."""
from marine.storage import canonical
from test_api import client as client


def seed(client, name, parents=(), *, tenant='demo', history=None):
    value={'batchId':name,'tenant':tenant,'ownerMSP':'Org1MSP','revision':1,'state':'PROCESSED',
           'speciesId':'saithe','massGrams':100,'parents':list(parents),'history':history or []}
    with client.app.state.store.connect() as db:
        db.execute('INSERT INTO batches VALUES(?,?,?,?)',(tenant,name,canonical(value).decode(),1))


def test_lineage_keeps_ancestry_and_impact_distinct_and_never_mutates(client):
    seed(client,'root');seed(client,'other');seed(client,'child',['root','other']);seed(client,'leaf',['child'])
    with client.app.state.store.connect() as db:
        before=[tuple(row) for row in db.execute('SELECT * FROM batches ORDER BY id')]
    downstream=client.get('/v1/batches/root/lineage?direction=descendants')
    assert downstream.status_code==200
    data=downstream.json();assert {row['batchId'] for row in data['nodes']}=={'root','child','leaf'}
    assert data['complete'] and not data['cycleDetected'] and data['mutated'] is False and data['ledgerVerified'] is False
    assert 'other' not in {row['batchId'] for row in data['nodes']}, 'a co-input is not a downstream affected batch'
    upstream=client.get('/v1/batches/leaf/lineage?direction=ancestors').json()
    assert {row['batchId'] for row in upstream['nodes']}=={'root','other','child','leaf'}
    with client.app.state.store.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM batches ORDER BY id')]==before
        assert db.execute('SELECT count(*) FROM ledger_jobs').fetchone()[0]==0


def test_lineage_foreign_missing_references_are_unresolved_without_identifier_disclosure(client):
    seed(client,'PRIVATE_FOREIGN',tenant='other');seed(client,'visible',['PRIVATE_FOREIGN'])
    assert client.get('/v1/batches/PRIVATE_FOREIGN/lineage').status_code==404
    response=client.get('/v1/batches/visible/lineage?direction=ancestors')
    assert response.status_code==200 and 'PRIVATE_FOREIGN' not in response.text
    data=response.json();assert not data['complete'] and data['unresolvedLinks']==1
    assert 'missing_links' in data['reasons'] and len(data['nodes'])==1


def test_lineage_detects_cycles_but_not_converging_multi_parent_paths(client):
    seed(client,'top');seed(client,'left',['top']);seed(client,'right',['top']);seed(client,'merge',['left','right'])
    good=client.get('/v1/batches/merge/lineage').json();assert good['complete'] and not good['cycleDetected']
    seed(client,'cycle-a',['cycle-b']);seed(client,'cycle-b',['cycle-a'])
    bad=client.get('/v1/batches/cycle-a/lineage').json();assert bad['cycleDetected'] and not bad['complete']
    assert 'cycle' in bad['reasons']


def test_lineage_limits_depth_nodes_and_large_payloads_without_false_empty_success(client):
    seed(client,'a');seed(client,'b',['a']);seed(client,'c',['b']);seed(client,'d',['c'])
    limited=client.get('/v1/batches/a/lineage?direction=descendants&max_depth=1').json()
    assert not limited['complete'] and 'depth_limit' in limited['reasons']
    assert {row['batchId'] for row in limited['nodes']}=={'a','b'}
    capped=client.get('/v1/batches/a/lineage?max_nodes=2').json()
    assert len(capped['nodes'])==2 and not capped['complete']
    ids={row['batchId'] for row in capped['nodes']}
    assert all(row['parent'] in ids and row['child'] in ids for row in capped['edges'])
    seed(client,'huge',history=[{'actor':'PRIVATE_HISTORY_SENTINEL'*5000}])
    response=client.get('/v1/batches/huge/lineage');assert response.status_code==200
    assert 'PRIVATE_HISTORY_SENTINEL' not in response.text
    assert not response.json()['complete'] and response.json()['nodes'][0]['available'] is False
    assert client.get('/v1/batches/a/lineage?max_nodes=101').status_code==422
    assert client.get('/v1/batches/a/lineage?max_depth=13').status_code==422


def test_lineage_scan_cap_is_explicit_and_ancestor_lookup_remains_indexed(client):
    seed(client,'z-root')
    for i in range(1001):seed(client,'filler-'+str(i).zfill(4))
    seed(client,'zz-child',['z-root'])
    partial=client.get('/v1/batches/z-root/lineage?direction=descendants').json()
    assert not partial['complete'] and 'scan_limit' in partial['reasons'] and partial['scanned']<=1000
    assert 'zz-child' not in {row['batchId'] for row in partial['nodes']}
    upstream=client.get('/v1/batches/zz-child/lineage?direction=ancestors').json()
    assert upstream['complete'] and upstream['scanned']==2


def test_invalid_legacy_row_does_not_create_a_false_complete_graph(client):
    seed(client,'root')
    with client.app.state.store.connect() as db:
        db.execute('INSERT INTO batches VALUES(?,?,?,?)',('demo',None,'{}',1))
    response=client.get('/v1/batches/root/lineage')
    assert response.status_code==200 and not response.json()['complete']
    assert 'metadata_unavailable' in response.json()['reasons']
