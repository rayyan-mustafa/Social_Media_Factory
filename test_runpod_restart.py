import urllib.request, json
import os

api_key = os.environ.get("RUNPOD_API_KEY", "")
endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID", "")

if api_key and endpoint_id:
    req = urllib.request.Request(
        'https://api.runpod.io/graphql',
        data=json.dumps({'query': f'mutation {{ endpointUpdate(id: "{endpoint_id}", input: {{idleTimeout: 5}}) {{ id }} }}'}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}
    )

    try:
        res = urllib.request.urlopen(req)
        print(res.read().decode())
    except Exception as e:
        print(e)
        if hasattr(e, 'read'):
            print(e.read().decode())
