# Frontend conventions (apps/web) — placeholder

Phase 3 (Angular with SSR: storefront at `/`, admin at `/admin`) will define these
conventions in its own plan. Nothing under `apps/web/` may be created before that plan is
approved.

Rules already known:

- **UI only**: no business rules, no database access. All data comes from the API through the
  client generated from `apps/api/openapi.json` (`packages/api-client`). Never hand-write
  request/response types or `fetch` calls against the API.
- Error handling uses the stable error `code` of `{code, message, details?}` to pick the
  user-facing message.
- Customer-facing copy is **Spanish**; code, comments and docs are English.
- **Your training data may be outdated for Angular.** Read the documentation of the installed
  version before using an API you are not sure about.
