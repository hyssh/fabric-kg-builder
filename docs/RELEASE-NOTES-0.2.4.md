# fabric-kg 0.2.4 Release Notes

Version 0.2.4 adds a strict L7 release transaction under
`fabric-kg app deploy-l7`. Planning is the default and performs no mutations.
Live execution requires the exact immutable plan hash, current configuration,
identity, tenant, unexpired approval window, and immediate readback agreement.
An explicitly authorized one-shot `--live` invocation generates and persists
the preflight plan and immediately consumes that same exact hash without a
prompt; `--approve-live` remains available for consuming a prior exact plan.

The narrowed live scope uses existing Fabric definition APIs, Azure AI Search,
Fabric Data Agent, and Foundry built-in project connections. Every enabled
mutation has a before/after journal and conditional rollback. Release-owned
names must begin with `fabric-kg-024-`; existing `surface-tech-*` and `ks3001`
resources cannot be adopted or deleted.

Existing Fabric items additionally require an immutable ownership receipt whose
stable ID, type, display name, definition hash, ETag, attempt, and authority
match live readback. The executor reserves receipt destinations before mutation
and does not report success until the immutable receipt is atomically committed
and fsynced.

Create intent is distinct from managed intent. Because current Fabric
create/delete APIs do not document ETag/CAS rollback, the live Azure adapter
fails first-create preflight rather than risk deleting a concurrently changed
item.

RemoteTool hosting, distributed Blob-lease L6 authority, signer
rotation/revocation, and RDF serialization are deferred. The canonical L6
five-tool definition remains generated and local in this release.

The supported acceptance architecture is a base-wheel `fabric-kg` subprocess
invoked locally by GitHub Copilot with `deploy_builtin_agent=false`. Hosted
RemoteTool/app extras, public endpoint, audience, and app-role are optional
future integration rather than 0.2.4 requirements.
