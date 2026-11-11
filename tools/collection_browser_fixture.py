"""Isolated synthetic collection fixture. No model, hardware or cloud calls."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient


def main():
    if os.environ.get('MARINE_SYNTHETIC_CHECK') != '1':
        raise SystemExit('Explicit synthetic flag required')
    root = Path(os.environ['COLLECTION_FIXTURE_ROOT']).resolve()
    port = int(os.environ.get('COLLECTION_FIXTURE_PORT', '18871'))
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    for key in list(os.environ):
        if key.startswith(('MARINE_', 'KAFKA_', 'IPFS_')):
            del os.environ[key]
    os.environ['MARINE_MODE'] = 'local'
    from marine.api import create_app
    app = create_app(root)
    with TestClient(app) as client:
        actor = client.post('/v1/session', headers={'X-Requested-With': 'marine-web'}).json()
        client.headers['X-Marine-Scope'] = actor['scopeHash']
        assert client.put('/v1/sites/collection-A', json={'name': '合成作业站点', 'longitude': 10, 'latitude': 20, 'dataSource': 'synthetic'}).status_code == 200
        assert client.put('/v1/devices/collection-device', json={'siteId': 'collection-A', 'enabled': True}).status_code == 200
        now = int(time.time())
        fish = json.loads(Path('contracts/examples.json').read_text())[0]
        fish.update(tenantId='demo', siteId='collection-A', deviceId='collection-device',
                    occurredAt=datetime.fromtimestamp(now-130, timezone.utc).isoformat().replace('+00:00', 'Z'))
        assert client.post('/v1/event-batches', json={'events': [fish]}).status_code == 202
        assert client.post('/v1/environment-readings', json={'requestId': 'synthetic-collection-env', 'deviceId': 'collection-device',
                           'occurredAt': now-120, 'temperatureC': 15, 'salinityPsu': 30, 'turbidityNtu': 1}).status_code == 200
        (root/'synthetic-fixture.json').write_text(json.dumps({'now': now, 'species': fish['data']['speciesId']}))
    uvicorn.run(create_app(root), host='127.0.0.1', port=port, access_log=False)


if __name__ == '__main__':
    main()
