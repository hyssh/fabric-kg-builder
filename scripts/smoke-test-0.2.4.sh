#!/usr/bin/env bash
set -euo pipefail

command -v uv >/dev/null
command -v jq >/dev/null
command -v shasum >/dev/null

repo="$(git rev-parse --show-toplevel)"
outside="$(mktemp -d "${TMPDIR:-/tmp}/fabric-kg-024.XXXXXX")"
trap 'chmod -R u+w "$outside" 2>/dev/null || true; rm -rf "$outside"' EXIT

git -C "$repo" archive --format=tar HEAD | tar -xf - -C "$outside"
uv build "$outside" --out-dir "$outside/dist" >/dev/null
wheel="$(find "$outside/dist" -maxdepth 1 -name 'fabric_kg_builder-0.2.4-*.whl' -print -quit)"
test -n "$wheel"

uv venv --python 3.12 "$outside/venv" >/dev/null
uv pip install --python "$outside/venv/bin/python" --quiet "$wheel"
unset PYTHONPATH
cd "$outside"

cli="$outside/venv/bin/fabric-kg"
test "$("$cli" --version)" = "fabric-kg, version 0.2.4"
test "$("$cli" --help | sed -n '/^Commands:/,$p' | grep -Ec '^  [a-z0-9-]+[[:space:]]{2,}')" -eq 36
origin="$("$outside/venv/bin/python" -c 'import fabric_kg_builder; print(fabric_kg_builder.__file__)')"
case "$origin" in
  "$outside/venv"/*) ;;
  *) echo "package origin escaped external venv: $origin" >&2; exit 1 ;;
esac

mkdir -p smoke
printf '%s\n' '{"definition_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}' > smoke/l6.json
printf '%s\n' '{"definition":{"parts":[]}}' > smoke/data-agent.json
printf '%s\n' '{"name":"ignored","fields":[{"name":"id","type":"Edm.String"}]}' > smoke/index.json
printf '%s\n' '{"documents":[]}' > smoke/docs.json

bytes_hash() { shasum -a 256 "$1" | awk '{print $1}'; }
json_hash() { jq -S -c . "$1" | tr -d '\n' | shasum -a 256 | awk '{print $1}'; }

jq -n \
  --arg l6b "$(bytes_hash smoke/l6.json)" --arg l6c "$(json_hash smoke/l6.json)" \
  --arg dab "$(bytes_hash smoke/data-agent.json)" --arg dac "$(json_hash smoke/data-agent.json)" \
  --arg isb "$(bytes_hash smoke/index.json)" --arg isc "$(json_hash smoke/index.json)" \
  --arg db "$(bytes_hash smoke/docs.json)" --arg dc "$(json_hash smoke/docs.json)" \
  '{
    release:"0.2.4", tenant_id:"tenant", subscription_id:"subscription",
    resource_group:"resource-group", expected_principal_id:"principal",
    fabric_workspace_id:"workspace", authority_hash:("1"*64),
    l5a_definition_hash:("2"*64), l5b_definition_hash:("3"*64),
    l6_definition:{path:"l6.json",sha256:$l6b,canonical_hash:$l6c},
    fabric_definitions:[{
      name:"fabric-kg-024-data-agent",item_id:"data-agent",
      item_type:"DataAgent",
      artifact:{path:"data-agent.json",sha256:$dab,canonical_hash:$dac}
    }],
    search:{
      endpoint:"https://example.search.windows.net",
      index_name:"fabric-kg-024-index",
      index_schema:{path:"index.json",sha256:$isb,canonical_hash:$isc},
      documents:{path:"docs.json",sha256:$db,canonical_hash:$dc},
      knowledge_source_name:"fabric-kg-024-source",
      knowledge_base_name:"fabric-kg-024-kb"
    },
    foundry:{
      account_name:"account",project_name:"project",
      search_connection_name:"fabric-kg-024-search",
      fabric_connection_name:"fabric-kg-024-fabric",
      data_agent_id:"data-agent",deploy_builtin_agent:false
    }
  }' > smoke/config.json

jq -n --arg dh "$(jq -S -c '.definition' smoke/data-agent.json | tr -d '\n' | shasum -a 256 | awk '{print $1}')" '{
  identity:{tenant_id:"tenant",principal_id:"principal"},
  resources:[{
    resource_id:"/workspaces/workspace/dataAgents/data-agent",
    exists:true,resource_type:"DataAgent",name:"fabric-kg-024-data-agent",
    etag:"etag",definition_hash:$dh
  }],
  capabilities:{
    "fabric.DataAgent.definition":true,
    "search.index":true,
    "search.knowledge-source":true,
    "search.knowledge-base":true,
    "foundry.project-connections":true
  },
  observed_at:"2026-08-27T00:00:00Z"
}' > smoke/observation.json

"$cli" app deploy-l7 \
  --config smoke/config.json \
  --observation smoke/observation.json \
  --plan smoke/plan.json
jq -e '.release == "0.2.4" and .l6_hosting == "generated-local-deferred"' \
  smoke/plan.json >/dev/null
test ! -w smoke/plan.json

if "$cli" app deploy-l7 \
  --config smoke/config.json \
  --observation smoke/observation.json \
  --live --approve-live invalid 2>/dev/null; then
  echo "live mode accepted a local observation" >&2
  exit 1
fi

echo "fabric-kg 0.2.4 external Python 3.12 CLI smoke passed"
