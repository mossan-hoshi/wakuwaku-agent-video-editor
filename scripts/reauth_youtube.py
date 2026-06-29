"""YouTube OAuth を read+upload スコープで再認証し、新しい refresh token を .env に書き戻す。

現状の refresh token は ``youtube.upload`` 専用で、既存動画の概要欄取得など読取ができない。
本スクリプトを一度実行すると、ブラウザ同意1回で ``youtube.readonly`` + ``youtube.upload`` の
refresh token を取得し、``.env`` の ``WWEDIT_YT_REFRESH_TOKEN`` を**自動で更新**する
（トークンは標準出力に出さない＝秘匿のまま）。投稿(upload)も引き続き使える。

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
