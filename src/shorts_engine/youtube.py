"""YouTube upload (OAuth) and stats lookup."""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
RETRY_STATUS = {500, 502, 503, 504}


class QuotaExceeded(RuntimeError):
    pass


def _clean(text: str, limit: int) -> str:
    return text.replace("<", "").replace(">", "").strip()[:limit]


def _fit_tags(tags: list[str], budget: int = 450) -> list[str]:
    out, used = [], 0
    for t in dict.fromkeys(t.strip() for t in tags if t.strip()):
        cost = len(t) + (2 if " " in t else 0) + 1
        if used + cost > budget:
            break
        out.append(t[:100])
        used += cost
    return out


class YouTube:
    def __init__(self, settings: dict, root: Path):
        self.settings = settings
        self.client_secrets = root / settings.get("client_secrets", "secrets/client_secret.json")
        self.token_file = root / settings.get("token_file", "secrets/token.json")
        self.channel_id = (settings.get("channel_id") or "").strip()
        self._svc = None

    def credentials(self, interactive: bool = False, force: bool = False):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = None
        if self.token_file.exists() and not force:
            creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                if not interactive:
                    raise RuntimeError(f"YouTube login expired or revoked ({e}). Run `shorts-engine auth`.") from e
                creds = None
        if not creds or not creds.valid:
            if not interactive:
                raise RuntimeError("YouTube not authorised. Run `shorts-engine auth` first.")
            if not self.client_secrets.exists():
                raise RuntimeError(f"OAuth client file missing: {self.client_secrets} (see docs/youtube-setup.md)")
            import json

            if "installed" not in json.loads(self.client_secrets.read_text()):
                raise RuntimeError("OAuth client must be type 'Desktop app' (this JSON is a Web client). "
                                   "Create a Desktop app client and download its JSON.")
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secrets), SCOPES)
            extra = {"login_hint": self.settings["account"]} if self.settings.get("account") else {}
            creds = flow.run_local_server(port=8765, open_browser=True, prompt="consent select_account", **extra)
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(creds.to_json())
        self.token_file.chmod(0o600)
        return creds

    @property
    def svc(self):
        if self._svc is None:
            from googleapiclient.discovery import build

            self._svc = build("youtube", "v3", credentials=self.credentials(), cache_discovery=False)
        return self._svc

    def channel(self) -> dict:
        """The channel the stored login uploads to: {id, title, handle}."""
        items = self.svc.channels().list(part="snippet", mine=True).execute().get("items", [])
        if not items:
            raise RuntimeError("This Google login has no YouTube channel. Re-run `shorts-engine auth` and pick the channel.")
        sn = items[0]["snippet"]
        return {"id": items[0]["id"], "title": sn.get("title", ""), "handle": sn.get("customUrl", "")}

    def verify_channel(self) -> dict:
        ch = self.channel()
        if self.channel_id and ch["id"] != self.channel_id:
            raise RuntimeError(
                f"Logged in as channel '{ch['title']}' ({ch['id']}), but config expects {self.channel_id}. "
                "Run `shorts-engine auth` and choose the right channel."
            )
        return ch

    def upload(self, file_path: Path, title: str, description: str, tags: list[str],
               publish_at: datetime, synthetic: bool) -> str:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        s = self.settings
        if "#shorts" not in description.lower():
            description = f"{description}\n\n#Shorts".strip()
        body = {
            "snippet": {
                "title": _clean(title, 100),
                "description": _clean(description, 4900),
                "tags": _fit_tags([*tags, *s.get("extra_tags", [])]),
                "categoryId": str(s.get("category_id", "24")),
                "defaultLanguage": s.get("default_language", "en"),
            },
            "status": {
                "privacyStatus": "private",  # required for scheduled publishing
                "publishAt": publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "selfDeclaredMadeForKids": bool(s.get("made_for_kids", False)),
                "containsSyntheticMedia": bool(synthetic),
            },
        }
        media = MediaFileUpload(str(file_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        req = self.svc.videos().insert(part="snippet,status", body=body, media_body=media,
                                       notifySubscribers=bool(s.get("notify_subscribers", False)))
        response, attempt = None, 0
        while response is None:
            try:
                _, response = req.next_chunk()
            except HttpError as e:
                reason = str(e)
                if e.status_code == 403 and ("quotaExceeded" in reason or "uploadLimitExceeded" in reason):
                    raise QuotaExceeded(reason) from e
                if e.status_code not in RETRY_STATUS or attempt >= 6:
                    raise
                attempt += 1
                time.sleep(min(2 ** attempt + random.random(), 60))
            except (ConnectionError, TimeoutError, OSError):
                if attempt >= 6:
                    raise
                attempt += 1
                time.sleep(min(2 ** attempt + random.random(), 60))
        return response["id"]

    def stats(self, video_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(video_ids), 50):
            resp = self.svc.videos().list(part="statistics", id=",".join(video_ids[i : i + 50])).execute()
            for item in resp.get("items", []):
                st = item.get("statistics", {})
                out[item["id"]] = {k: int(st.get(f"{k[:-1]}Count", 0)) for k in ("views", "likes", "comments")}
        return out
