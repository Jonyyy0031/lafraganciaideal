---
status: open # open → deferred | planned | resolved | discarded (the user decides)
module: platform
found: 2026-10-20
---

# Per-IP limits depend on an undocumented proxy setting

## Found while

Reviewing orders-checkout/005, the order lookup throttle.

## What

- **Every per-IP limit keys on `request.client.host`.** That covers:
  - the order lookup (`apps/api/src/fragancia_api/modules/orders/http/router.py:411`);
  - identity's login and password reset (`modules/identity/http/router.py:110`, `:223`).
- **What uvicorn 0.54 does.** It runs `ProxyHeadersMiddleware` by default and trusts
  `X-Forwarded-For` only from `FORWARDED_ALLOW_IPS`, which defaults to `127.0.0.1,::1`. It reads
  the header from the right.
- **No deployment doc** says how the proxy and that setting must be configured.

## Why it matters

- **A proxy in another container or host, with the setting unset.** Every client shows up as
  the proxy's IP. Each per-IP limit becomes global, so one client can lock everyone out of the
  login and the order lookup:
  - 50 login attempts in 15 minutes;
  - 20 order lookups in 15 minutes.
- **`FORWARDED_ALLOW_IPS="*"`.** Any client sets its own `X-Forwarded-For` and gets a fresh key
  per request. The per-IP limits then stop working. The per-email and per-number limits still
  hold.

## Suggested next step

The deployment plan should do two things:
- set `--forwarded-allow-ips` to the proxy's exact address, with a proxy that appends
  `X-Forwarded-For`;
- document it, and add a smoke check that the API sees the real client IP.
