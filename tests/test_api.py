import os
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

class SmokeTests(unittest.TestCase):
    def test_workflow_and_usage(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ['AGENT_MONITOR_DATA']=td
            from monitor.app import app, KEY
            with TestClient(app) as client:
                h={'X-Monitor-Key':KEY}
                self.assertEqual(client.get('/').status_code,200)
                self.assertEqual(client.post('/api/runs',json={'title':'No key'}).status_code,401)
                rid=client.post('/api/runs',headers=h,json={'title':'Test'}).json()['id']
                nid=client.post(f'/api/runs/{rid}/nodes',headers=h,json={'title':'Step','agent':'Codex'}).json()['id']
                self.assertEqual(client.post(f'/api/runs/{rid}/events',headers=h,json={'type':'node.status','node_id':nid,'status':'running'}).status_code,200)
                result=client.post(f'/api/runs/{rid}/events',headers=h,json={'type':'usage','node_id':nid,'provider':'codex','usage':{'session_id':'x','usage_key':'t1','input_tokens':20,'output_tokens':10,'cost_source':'unavailable'}})
                self.assertEqual(result.status_code,200)
                snap=client.get(f'/api/runs/{rid}').json()
                self.assertEqual(snap['usage'][0]['input_tokens'],20)
                self.assertEqual(snap['nodes'][0]['status'],'running')

if __name__=='__main__': unittest.main()
