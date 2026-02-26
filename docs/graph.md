# Graph Features

Open Timeline Engine (TCE) is timeline-first, with a graph layer that enriches retrieval and context bundles.

## What gets extracted

On event ingest, the graph indexer extracts candidate entities from:

- `domain`
- `task_type`
- `title`
- `tags`
- selected `context` fields (`project`, `repo`, `branch`)
- selected `payload` fields (`summary`, link references)

Entity classification buckets:

- `domain`
- `path`
- `url`
- `concept`

## Relationship types

Open Timeline Engine currently records:

- `follows`: sequence relation to the previous event in the same `domain + task_type` scope
- `contradicts`: emitted when a new fact assertion conflicts with the latest active value

## Fact assertions and conflict resolution

If an event payload contains `facts`:

- dict form: `{ "key": "value" }`
- list form: `[{"key":"...","value":"..."}]`

then Open Timeline Engine writes normalized fact assertions scoped by workspace/user/domain.

When a newer value conflicts with an active prior value:

- prior assertion is marked inactive
- new assertion is active
- a `contradicts` relationship is created from new event to superseded event

## APIs

- `GET /v1/graph/entities?query=<q>&k=<n>`
- `GET /v1/graph/event/{event_id}`

MCP equivalents:

- `tce.search_entities`
- `tce.get_event_graph`

## Graph effect on search

Search uses lexical + optional vector + graph boost.

- events linked to matched entities receive an additional graph boost
- graph is scoped by workspace/user headers to prevent cross-scope leakage

## Scope and safety

Graph data is scoped using:

- `X-TCE-Workspace`
- `X-TCE-User`

Default safety still applies:

- sensitivity `3` blocked by default
- redaction and policy enforcement before outputs
