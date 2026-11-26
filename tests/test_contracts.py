from pathlib import Path
import yaml, pytest
from shared.core import resolve, validate
ROOT=Path(__file__).resolve().parents[1]
@pytest.mark.parametrize('path',sorted((ROOT/'api').glob('*-service.yaml')),ids=lambda p:p.stem)
def test_openapi_examples_and_refs(path):
    spec=yaml.safe_load(path.read_text());resolve(spec,spec);ids=set()
    for route,methods in spec['paths'].items():
        for method,op in methods.items():
            assert op['operationId'] not in ids;ids.add(op['operationId'])
            if route.startswith('/internal/'):assert 'internal' in op['tags']
            for par in op.get('parameters',[]):
                p=resolve(spec,par)
                if 'example' in p:validate(spec,p['schema'],p['example'])
            if 'requestBody' in op:
                x=op['requestBody']['content']['application/json'];validate(spec,x['schema'],x['example'])
            for response in op['responses'].values():
                response=resolve(spec,response)
                for x in response.get('content',{}).values():
                    if 'example' in x:validate(spec,x['schema'],x['example'])

def test_event_examples():
    spec=yaml.safe_load((ROOT/'api/events.yaml').read_text());resolve(spec,spec)
    for name,m in spec['components']['messages'].items():
        for example in m['examples']:validate(spec,m['payload'],example['payload'])
