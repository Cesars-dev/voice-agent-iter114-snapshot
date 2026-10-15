# Update Call restricted to ended calls (08/31/2026)

**Source:** https://docs.retellai.com/deprecation-notice/2026/08-31_update_call_ended_calls_only

## What's changing

The `PATCH /v2/update-call/{call_id}` API is changing to operate on **ended calls only**.

1. Update Call will only accept requests for calls that have already ended. Requests targeting ongoing calls will be rejected.
2. Updates made through Update Call (including `data_storage_setting`) take effect only on ended calls, not on ongoing calls.
3. The `override_dynamic_variables` input on Update Call is **deprecated** and will no longer be accepted.

## Migration

### For ongoing (live) calls — use Update Live Call

**Before** (Update Call on live call):
```json
PATCH /v2/update-call/{call_id}
{
  "override_dynamic_variables": { "additional_discount": "15%" }
}
```

**After** (Update Live Call):
```json
PATCH /v2/update-live-call/{call_id}
{
  "fields_to_override": {
    "override_dynamic_variables": { "additional_discount": "15%" }
  }
}
```

The Update Live Call request can also override `metadata`, `data_storage_setting`, and control the live agent via `call_control` (`trigger_response`, `additional_context`).

### For ended calls — continue using Update Call

No change needed for `metadata`, `data_storage_setting`, or `custom_attributes` on ended calls.

Remove `override_dynamic_variables` from those requests — it has no effect on an ended call.

## Affected endpoints

- **PATCH /v2/update-call/{call_id}** — field: `override_dynamic_variables`

## Replacement endpoint

- **PATCH /v2/update-live-call/{call_id}** — field: `fields_to_override.override_dynamic_variables`

## Effective date

**08/31/2026** — Update Call will only work for ended calls.
