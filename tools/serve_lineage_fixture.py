"""Local synthetic lineage view. No ledger worker, provider or live field data."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import uvicorn
from marine.api import create_app
from marine.storage import canonical


def main():
    if os.getenv('MARINE_SYNTHETIC_CHECK') != '1':
        raise SystemExit('Explicit synthetic-check flag required')
    with TemporaryDirectory(prefix='marine-lineage-') as directory:
        app=create_app(Path(directory))
        with app.state.store.connect() as db:
            for name,parents in [('root',[]),('other',[]),('child',['root','other']),('leaf',['child'])]:
                value={'batchId':name,'tenant':'demo','ownerMSP':'Org1MSP','revision':1,'state':'PROCESSED',
                    'speciesId':'saithe','massGrams':100,'areaCode':'synthetic','parents':parents,
                    'history':[{'actor':'PRIVATE_SYNTHETIC_HISTORY'}], 'chainStatus':'NOT_SUBMITTED'}
                db.execute('INSERT INTO batches VALUES(?,?,?,?)',('demo',name,canonical(value).decode(),1))
        uvicorn.run(app,host='127.0.0.1',port=int(os.getenv('MARINE_PREVIEW_PORT','19301')),access_log=False)


if __name__=='__main__':main()
