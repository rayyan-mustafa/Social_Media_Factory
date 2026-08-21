import os
import runpod

runpod.api_key = os.environ.get("RUNPOD_API_KEY", "")
endpoint_id = os.environ.get("RUNPOD_ENDPOINT_ID", "")
if runpod.api_key and endpoint_id:
    print(runpod.api.graphql.run_graphql_query(f'query {{ endpoint(id: "{endpoint_id}") {{ templateId }} }}'))
