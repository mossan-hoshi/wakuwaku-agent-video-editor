"""[K] YouTube 投稿（Data API v3 videos.insert）。

リクエスト本体の組み立て(`build_video_resource`)は**純関数＝テスト可能・キー不要**。
実投稿(`upload_video`)は google-api-python-client ＋ OAuth refresh token が要るため
遅延 import し、無ければ手順を示して落ちる（dry-run で本体JSONだけ先に検証できる）。

認証情報は **.env のみ**（コード非直書き）:
  WWEDIT_YT_CLIENT_ID / WWEDIT_YT_CLIENT_SECRET / WWEDIT_YT_REFRESH_TOKEN
"""

from __future__ import annotations

# Science & Technology
DEFAULT_CATEGORY_ID = "28"
# tags の合計文字数上限（YouTube Data API）。超過分は落とす。
TAGS_TOTAL_LIMIT = 480


def tags_from_description(description: str, *, total_limit: int = TAGS_TOTAL_LIMIT) -> list[str]:
    """概要欄の **#ハッシュタグ行**から tags を起こす（``#`` を外し順序維持・重複除去）。

    固定の既定タグを付けると、その回の内容と無関係な語が混ざる（#100 で実際に起きた）。
    その回の検索語は概要欄のハッシュタグ行＝**内容に合わせて毎回決めるもの**で表現されている。
    固定の既定タグを付けると内容と無関係な語が入る（#100 で「コンピュータビジョン」「論文紹介」が
    付いてしまった）ため、**tags は概要欄のハッシュタグから起こす**のを既定にする。
    """
    tags: list[str] = []
    seen: set[str] = set()
    total = 0
    for line in description.splitlines():
        words = line.split()
        if not words or not all(w.startswith("#") for w in words):
            continue  # ハッシュタグだけの行が対象（本文中の # は拾わない）
        for w in words:
            t = w.lstrip("#").strip()
            if not t or t.lower() in seen:
                continue
            # tags は合計文字数に上限がある。空白を含む語は引用符扱いで2字ぶん多く数えられる。
            cost = len(t) + (2 if " " in t else 0) + 1
            if total + cost > total_limit:
                return tags
            seen.add(t.lower())
            tags.append(t)
            total += cost
    return tags


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
    5000字の API 上限でトリム。**tags 未指定は概要欄のハッシュタグ行から起こす**
    （`tags_from_description`＝内容に合ったタグになる）。``tags=[]`` で明示的にタグ無し。
    """
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError(f"privacy は private/unlisted/public: {privacy}")
    return {
        "snippet": {
            "title": title.strip()[:100],
            "description": description[:5000],
            "tags": tags if tags is not None else tags_from_description(description),
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
        },
    }


def _oauth_credentials():
    """.env の refresh token から認証情報を作る（遅延 import）。無ければ分かるエラー。"""
    from wwedit.common.env import env_value

    cid = env_value("WWEDIT_YT_CLIENT_ID")
    secret = env_value("WWEDIT_YT_CLIENT_SECRET")
    refresh = env_value("WWEDIT_YT_REFRESH_TOKEN")
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
        # upload だけを並べると、readonly を持つ refresh token でも
        # **upload 限定のアクセストークン**が発行され `videos.list` が 403 になる。
        # 再認証(scripts/reauth_youtube.py)で付与した scope をそのまま並べる。
        scopes=[
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ],
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


# サムネの API 上限（超えると 400。PNG/JPEG いずれも同じ）。
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024


def set_thumbnail(video_id: str, image_path: str) -> dict:
    """投稿済み動画にカスタムサムネを設定する（`thumbnails.set`）。

    **2MB 上限**があり `publish thumbnail` の出力(2K PNG)はたいてい超えるので、
    超過時は JPEG へ品質を落として変換してから送る（元ファイルは触らない）。
    """
    from pathlib import Path

    src = Path(image_path)
    if not src.exists():
        raise FileNotFoundError(image_path)
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:
        raise RuntimeError(
            "google-api-python-client が未導入です: "
            "`uv add google-api-python-client google-auth-oauthlib`"
        ) from e

    send, mime = src, ("image/png" if src.suffix.lower() == ".png" else "image/jpeg")
    if src.stat().st_size > THUMBNAIL_MAX_BYTES:
        send, mime = _shrink_thumbnail(src), "image/jpeg"

    yt = build("youtube", "v3", credentials=_oauth_credentials())
    return yt.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(str(send), mimetype=mime, resumable=False),
    ).execute()


def _shrink_thumbnail(src, max_bytes: int = THUMBNAIL_MAX_BYTES):
    """2MB 以内に収まる JPEG を隣に作って返す（1280x720・品質を段階的に落とす）。"""
    from PIL import Image

    out = src.with_name(f"{src.stem}_yt.jpg")
    im = Image.open(src).convert("RGB")
    if im.width > 1280:
        im = im.resize((1280, round(im.height * 1280 / im.width)), Image.LANCZOS)
    for q in (92, 85, 78, 70, 60):
        im.save(out, "JPEG", quality=q, optimize=True)
        if out.stat().st_size <= max_bytes:
            return out
    return out
