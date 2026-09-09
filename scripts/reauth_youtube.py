"""YouTube OAuth を再認証し、新しい refresh token を .env に書き戻す。

ブラウザ同意1回で下の3スコープの refresh token を取得し、``.env`` の
``WWEDIT_YT_REFRESH_TOKEN`` を**自動で更新**する（トークンは標準出力に出さない＝秘匿のまま）。

🚨 **``youtube.force-ssl`` が要る**（2026-08-08 追加）。``upload`` だけだと
**投稿した後から直せない**——投稿後に概要欄の誤りが見つかっても `videos.update` が
403「Insufficient Permission」で落ち、サムネの `thumbnails.set` も同じ理由で落ちる。
実害: 5本を投稿した後にライセンス表記漏れとタグ漏れが判明し、API では直せなかった。

| スコープ | できること |
|---|---|
| `youtube.upload` | 投稿（videos.insert） |
| `youtube.readonly` | 既存動画の読み取り（videos.list） |
| **`youtube.force-ssl`** | **概要欄/タイトル/タグの更新（videos.update）・サムネ設定（thumbnails.set）** |

実行: リポジトリ直下で
    uv run --no-sync python scripts/reauth_youtube.py
ブラウザが開くので、チャンネルのGoogleアカウントでログイン→アクセスを許可。
"""

from __future__ import annotations

from pathlib import Path

from wwedit.common.env import env_value

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def _update_env(env_path: Path, key: str, value: str) -> None:
    """.env の key 行を value に置換（無ければ追記）。他行は保持。"""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    cid = env_value("WWEDIT_YT_CLIENT_ID")
    secret = env_value("WWEDIT_YT_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("WWEDIT_YT_CLIENT_ID / WWEDIT_YT_CLIENT_SECRET が .env にありません")

    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": cid,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    # offline + consent で必ず refresh token を得る
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        raise SystemExit("refresh token が取得できませんでした（同意画面で許可されたか確認）")

    _update_env(Path(".env"), "WWEDIT_YT_REFRESH_TOKEN", creds.refresh_token)
    print("OK: .env の WWEDIT_YT_REFRESH_TOKEN を更新しました（read+upload 有効）。")
    print("付与スコープ:", ", ".join(creds.scopes or SCOPES))


if __name__ == "__main__":
    main()
