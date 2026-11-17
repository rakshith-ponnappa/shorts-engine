# Security policy

## Supported versions

shorts-engine is early-stage. Only the latest commit on `main` gets security fixes.

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately through GitHub's
[private vulnerability reporting](https://github.com/rakshith-ponnappa/shorts-engine/security/advisories/new)
("Report a vulnerability" on the Security tab). Include steps to reproduce and the impact. You should get an
acknowledgement within 7 days.

## Handling credentials

This tool works with several secrets. Keep all of them out of version control:

| Secret | Where it lives | Notes |
|---|---|---|
| OpenAI / Azure OpenAI keys | `.env` | gitignored; `deploy/gcloud-setup.sh` sets it to mode `600` |
| YouTube Data API key | `.env` (`YOUTUBE_API_KEY`) | restrict it to the YouTube Data API v3 in the Cloud Console |
| OAuth client JSON | `secrets/client_secret.json` | Desktop app client; `secrets/` is gitignored and created with mode `700` |
| OAuth refresh token | `secrets/token.json` | grants upload access to your channel; treat it like a password |

If a secret leaks:

1. Revoke it right away: delete or rotate the API key in the Cloud Console, rotate the OpenAI/Azure key,
   and remove the app's access at <https://myaccount.google.com/permissions> (this invalidates `token.json`).
2. Delete the OAuth client and create a new one if `client_secret.json` was exposed.
3. Purge it from git history (for example with `git filter-repo`). Rotating it is still required.

Run a secret scanner such as [gitleaks](https://github.com/gitleaks/gitleaks) before pushing:

```bash
gitleaks dir . --redact
gitleaks git . --redact
```
