# V1 → V2 migration

Do not copy the whole V1 project over V2.

Copy only configuration values you still need:

- `APPS_SCRIPT_URL`
- `APPS_SCRIPT_TOKEN`
- any deliberate browser/channel preference

Keep `PUBLISH_MODE=review` for the first V2 tests.

Do **not** copy these into a shared release:

- `.env` from a machine you do not control
- `data\indiamart_storage_state.json`
- old Chrome profiles
- `venv` / `.venv`
- `outputs`
- `__pycache__`

V2 intentionally asks you to prepare a product again rather than silently reinterpret a V1 `prepared_product.json`. This prevents stale model/spec/company state from contaminating the new canonical record.
