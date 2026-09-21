# Security and secret handling

## API Key storage

- Production API keys must be stored only in the hosting provider's secret environment variable `VI_API_KEY`.
- The browser receives only `has_key: true/false`; it never receives the key itself.
- Public deployments reject attempts to write API keys through `/api/config`.
- Application logs do not record request bodies, user links, cookies, authorization headers, or provider response bodies.

## Sharing the source code

Do not compress the working directory manually. Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-safe-release.ps1
```

The command uses `git archive`, so only committed files are included. Local configuration, `.env` files, cookies, downloaded videos, logs, Git history and untracked test artifacts are excluded.

## If a key may have leaked

Deleting it from a file is not sufficient. Revoke the old key in the provider console, create a new key, update the hosting secret, and redeploy. Never send a real key in screenshots, chat messages, issue reports, or test fixtures.
