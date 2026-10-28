"""Reopen the existing isolated native-model provider for browser switching QA."""
import json
import os
from pathlib import Path

from accept_local_search import configure

HOME = Path('D:/code/rynmesh-local-ai-acceptance-c124bb62d6654226903a51badd14d528')


if __name__ == '__main__':
    assert (HOME / 'llm/provider-settings.json').is_file()
    configure(HOME, 18846)
    os.environ.update(RYNMESH_LLM_HOME=str(HOME / 'llm'), RYNMESH_PEER_ENDPOINT='http://127.0.0.1:18846',
                      RYNMESH_PEER_PORT='18846')
    import uvicorn

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    app = create_app(RynmeshStore(home=HOME, network_dir=HOME / 'network', node_name='Switch acceptance provider'))
    (HOME / '.switch-provider-fixture.json').write_text(json.dumps({'pid': os.getpid(), 'port': 18846}) + '\n')
    uvicorn.run(app, host='127.0.0.1', port=18846, access_log=False)
