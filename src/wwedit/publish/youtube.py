"""[K] YouTube 投稿（Data API v3 videos.insert）。

リクエスト本体の組み立て(`build_video_resource`)は**純関数＝テスト可能・キー不要**。
実投稿(`upload_video`)は google-api-python-client ＋ OAuth refresh token が要るため
遅延 import し、無ければ手順を示して落ちる（dry-run で本体JSONだけ先に検証できる）。

認証情報は **.env のみ**（コード非直書き）:
  WWEDIT_YT_CLIENT_ID / WWEDIT_YT_CLIENT_SECRET / WWEDIT_YT_REFRESH_TOKEN
"""

from __future__ import annotations

import os

# Science & Technology
DEFAULT_CATEGORY_ID = "28"
DEFAULT_TAGS = ["勉強会", "AI", "コンピュータビジョン", "論文紹介", "わくわくべんきょ会"]


def build_video_resource(
    title: str,
    description: str,
    *,
    tags: list[str] | None = None,
    category_id: str = DEFAULT_CATEGORY_ID,
    privacy: str = "private",
    made_for_kids: bool = False,
) -> dict:
    """videos.insert の body（snippet＋status）を組み立てる純関数。

    privacy: private(既定・下書き相当) / unlisted / public。title は100字・description は
    5000字の API 上限でトリム。tags 未指定は既定タグ。
    """
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError(f"privacy は private/unlisted/public: {privacy}")
    return {
        "snippet": {
            "title": title.strip()[:100],
            "description": description[:5000],
            "tags": tags if tags is not None else list(DEFAULT_TAGS),
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }


def _oauth_credentials():
    """.env の refresh token から認証情報を作る（遅延 import）。無ければ分かるエラー。"""
    cid = os.environ.get("WWEDIT_YT_CLIENT_ID")
    secret = os.environ.get("WWEDIT_YT_CLIENT_SECRET")
    refresh = os.environ.get("WWEDIT_YT_REFRESH_TOKEN")
    missing = [k for k, v in (("WWEDIT_YT_CLIENT_ID", cid),
                              ("WWEDIT_YT_CLIENT_SECRET", secret),
                              ("WWEDIT_YT_REFRESH_TOKEN", refresh)) if not v]
    if missing:
        raise RuntimeError(
            "YouTube 認証情報が .env にありません: " + ", ".join(missing)
            + "（OAuth で refresh token を発行し .env に設定してください）"
        )
    try:
        from google.oauth2.credentials import Credentials
    except ImportError as e:
        raise RuntimeError(
            "google-auth が未導入です: `uv add google-api-python-client google-auth-oauthlib`"
        ) from e
    return Credentials(
        token=None, refresh_token=refresh, client_id=cid, client_secret=secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )


def upload_video(video_path: str, body: dict) -> dict:
    """実際に動画をアップロードして API レスポンス（id 等）を返す。キー＋依存が要る。"""
    from pathlib import Path

    if not Path(video_path).exists():
        raise FileNotFoundError(video_path)
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:
        raise RuntimeError(
            "google-api-python-client が未導入です: "
            "`uv add google-api-python-client google-auth-oauthlib`"
        ) from e
    creds = _oauth_credentials()
    yt = build("youtube", "v3", credentials=creds)
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _status, resp = req.next_chunk()
    return resp
