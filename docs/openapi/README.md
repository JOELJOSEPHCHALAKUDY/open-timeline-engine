# OpenAPI Contracts

Export current API contracts with:

```bash
make openapi
```

Outputs:

- `docs/openapi/tce_api.v1.json`
- `docs/openapi/tce_lite_api.v1.json`

CI also exports these contracts during the integration workflow and validates the files are generated.
