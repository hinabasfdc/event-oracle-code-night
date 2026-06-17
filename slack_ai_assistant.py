"""
Slack AI Assistant — Oracle Private Agent Factory 連携

Slack の Agent コンテナ（上部バーからアクセスする AI アシスタント UI）で
ユーザーのメッセージを受け取り、Oracle AI Database Private Agent Factory に
問い合わせて応答を返す Slack Bot アプリケーション。

動作フロー:
  1. 起動時に Agent Factory へ Basic 認証でログインし、セッション Cookie を取得
  2. ユーザーが Slack Agent コンテナを開くと、ウェルカムメッセージを表示
  3. ユーザーがメッセージを送信すると:
     - Slack 上に「考え中…」ステータスを表示
     - Agent Factory の REST API にメッセージを転送
     - 応答を Markdown ブロック形式で Slack に返す（ソース引用付き）

必要な環境変数:
  .env:
    SLACK_BOT_TOKEN  - Slack Bot User OAuth Token (xoxb-...)
    SLACK_APP_TOKEN  - Slack App-Level Token (xapp-...)
    AF_USER          - Agent Factory の Basic 認証ユーザー名
    AF_PASS          - Agent Factory の Basic 認証パスワード
  .env.agents: (.env に全て書いてあっても良い)
    AF_AGENT_URL     - Agent Factory の完全な API URL


免責事項:
あくまで動作確認・サンプルのソースコードです。動作保証やサポートはありません。
"""
import logging
import os

import requests
import urllib3
from dotenv import load_dotenv
from slack_bolt import App, Assistant, Say, SetStatus
from slack_bolt.context.set_title import SetTitle
from slack_bolt.adapter.socket_mode import SocketModeHandler

# .env から Slack / Agent Factory の接続情報を読み込み、
# .env.agents から Agent の ID・種類を読み込む（分離することで Agent 切替が容易）
load_dotenv()
load_dotenv(".env.agents")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Agent Factory は自己署名証明書を使用しているため、証明書検証の警告を抑制
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =============================================================================
# Slack App 初期化
# Socket Mode で動作するため、HTTP エンドポイントの公開は不要
# =============================================================================
app = App(token=os.environ["SLACK_BOT_TOKEN"])

# =============================================================================
# Agent Factory 接続設定
# AF_AGENT_URL にはホスト・Agent種類・Agent IDを含む完全な URL を指定する
# 例: https://147.224.204.100:8080/agentFactory/v1/knowledge/run/AISTUDIO
# =============================================================================
AF_AGENT_URL = os.environ.get("AF_AGENT_URL", "").rstrip("/")
AF_USER = os.environ.get("AF_USER", "")
AF_PASS = os.environ.get("AF_PASS", "")

# Agent Factory との通信用セッション（ログイン後の Cookie を保持する）
af_session = requests.Session()
af_session.verify = False  # 自己署名証明書のため検証を無効化

# Slack スレッド (thread_ts) → Agent Factory Room ID のマッピング
# 同一スレッド内の会話を Agent Factory 側でも継続させるために使用
thread_room_map: dict[str, str] = {}


def _get_af_host() -> str:
    """AF_AGENT_URL からホスト部分を抽出する (例: https://xxx.xxx.xxx.xxx:8080)"""
    from urllib.parse import urlparse
    parsed = urlparse(AF_AGENT_URL)
    return f"{parsed.scheme}://{parsed.netloc}"


def af_login():
    """
    Agent Factory に Basic 認証でログインし、セッション Cookie を取得する。
    このセッション Cookie は以降の API 呼び出しで認証に使用される。
    アプリ起動時に一度だけ呼び出す。
    """
    host = _get_af_host()
    url = f"{host}/agentFactory/v1/loginValidation"
    r = af_session.get(url, auth=(AF_USER, AF_PASS))
    if r.status_code != 200:
        raise RuntimeError(f"AF ログイン失敗: HTTP {r.status_code}")
    if not af_session.cookies:
        raise RuntimeError("AF ログイン成功だがセッション Cookie なし")
    logger.info(f"AF ログイン成功 (cookies: {list(af_session.cookies.keys())})")


def af_ask(message: str, room_id: str | None = None) -> dict:
    """
    Agent Factory にメッセージを送信し、AI の応答を取得する。

    Args:
        message: ユーザーからの質問テキスト
        room_id: 会話の継続に使用する Room ID（初回は None、2回目以降は前回の応答から取得）

    Returns:
        Agent Factory の応答 JSON。主なキー:
        - message: AI の回答テキスト（Markdown 形式）
        - sources: 参考にしたドキュメントのリスト
        - roomId: 会話継続用の Room ID
    """
    payload = {"message": message}
    if room_id:
        payload["roomId"] = room_id

    r = af_session.post(AF_AGENT_URL, json=payload, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"AF エラー: HTTP {r.status_code}\n{r.text}")
    return r.json()


# =============================================================================
# Slack Assistant ハンドラ
# Slack の Agent コンテナ UI のライフサイクルイベントを処理する
# =============================================================================
assistant = Assistant()


@assistant.thread_started
def handle_thread_started(say: Say):
    """
    ユーザーが Agent コンテナを開いた時に呼ばれる。
    ウェルカムメッセージを表示して、ユーザーに入力を促す。
    """
    say("こんにちは！何かお手伝いできることはありますか？")


def format_response(data: dict) -> dict:
    """
    Agent Factory の応答を Slack Block Kit 形式に変換する。

    Slack の markdown ブロック（type: "markdown"）を使用することで、
    Agent Factory が返す標準 Markdown（テーブル、見出し、リスト等）を
    Slack が自動的にネイティブ表示に変換してくれる。

    構成:
      1. 本文: Agent Factory の回答テキスト（Markdown ブロック）
      2. ソース: 参考にしたドキュメントへのリンクリスト（Markdown ブロック）
      3. 免責事項: AI 生成であることの注意書き（context ブロック）
    """
    blocks = []

    # 本文: Agent Factory の回答をそのまま markdown ブロックとして表示
    message = data.get("message", "応答を取得できませんでした。")
    blocks.append({"type": "markdown", "text": message})

    # ソース: 応答の根拠となったドキュメントへのリンクを表示
    sources = data.get("sources") or []
    if sources:
        source_lines = ["---", "**参考ソース**"]
        for i, s in enumerate(sources, 1):
            title = s.get("title", "")
            url = s.get("url", "")
            if url:
                source_lines.append(f"{i}. [{title}]({url})")
            else:
                source_lines.append(f"{i}. {title}")
        blocks.append({"type": "markdown", "text": "\n".join(source_lines)})

    # 免責事項: AI 生成コンテンツであることをユーザーに明示
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": ":information_source: _AI による生成のため、不正確な情報が含まれる場合があります。_"}],
    })

    return {"blocks": blocks, "text": message}


@assistant.user_message
def handle_user_message(
    payload: dict,
    say: Say,
    set_status: SetStatus,
    set_title: SetTitle,
):
    """
    ユーザーが Agent コンテナでメッセージを送信した時に呼ばれる。

    処理の流れ:
      1. スレッドタイトルをユーザーのメッセージに設定（履歴タブでの検索用）
      2. 「考え中…」ステータスを表示（ユーザーに処理中であることを伝える）
      3. Agent Factory API にメッセージを送信（同一スレッドなら roomId を引き継ぐ）
      4. 応答を Block Kit 形式に整形して Slack に投稿
         （投稿すると「考え中…」ステータスは自動的にクリアされる）
    """

    print("[DEBUG] start handle_user_message") #デバッグ用

    user_message = payload.get("text", "")
    thread_ts = payload.get("thread_ts", "")
    set_title(user_message)
    set_status(status="考え中…")

    try:
        room_id = thread_room_map.get(thread_ts)
        data = af_ask(user_message, room_id=room_id)
        print(data) #デバッグ用

        # Agent Factory から返された roomId を保存して次回以降の会話で再利用
        if data.get("roomId") and thread_ts:
            thread_room_map[thread_ts] = data["roomId"]

        response = format_response(data)
        say(**response)
    except Exception as e:
        logger.exception(f"Agent Factory 呼び出し失敗: {e}")
        # エラー時はステータスを明示的にクリアし、エラーメッセージを表示
        set_status(status="")
        say(f":warning: エラーが発生しました: {e}")


# Assistant ハンドラを Slack App に登録
app.use(assistant)


# =============================================================================
# エントリーポイント
# =============================================================================
if __name__ == "__main__":
    # Agent Factory にログインしてセッションを確立
    af_login()
    # Slack Socket Mode で接続を開始（WebSocket 経由でイベントを受信）
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
