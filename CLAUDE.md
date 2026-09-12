# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概要

Web漫画の更新を Discord に通知するツール。**実行環境は GitHub Actions（毎日 JST 12:15 の1回）** であり、
ローカルPCは開発とデバッグにしか使わない。ローカルで動かないことは本番の異常を意味しない。

ドキュメント・コメント・コミットメッセージはすべて日本語で書く。

## 制約

- **外部ライブラリを使わない。** Python 標準ライブラリのみ。ワークフローに `pip install` のステップが無く、
  追加すると実行が遅くなるため。HTML解析も `html.parser`、RSS も `xml.etree` で済ませている。
- **`seen.json` は CI が書き換えてコミットする。** ローカルで作業する前に必ず `git pull --rebase` する。
  push が弾かれたら rebase してやり直す（force push はしない）。

## コマンド

```powershell
# 投稿せずに取得と差分検出だけ確認する（最もよく使う）
$env:PYTHONIOENCODING="utf-8"; $env:DRY_RUN="1"; python check.py

# 実際に Discord へ投稿して疎通を確認する
$env:DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."; $env:TEST_POST="1"; python check.py
```

Windows のコンソールでは日本語が化けるので `PYTHONIOENCODING=utf-8` を付ける。Actions 側は UTF-8 なので不要。

ローカル実行は `seen.json` を書き換えてしまう。確認が済んだら `git checkout -- seen.json` で戻す。

自動テストは無い。検証はドライランと、後述のスクラッチコピーによる手動確認で行う。

## アーキテクチャ

### 取得方式の切り替え

サイトごとに RSS の有無が違うため、`feeds.json` の `type` で取得関数を選ぶ（`SOURCES` 辞書）。
`type` 省略時は `rss`。

| type | 対象 | 取得元 |
|---|---|---|
| `rss`（既定） | GigaViewer 系（ジャンプ+、COMIC Y-OURS など） | `/rss/series/<id>` |
| `comicwalker` | カドコミ | 公開JSON API `/api/contents/details/work` |
| `yanmaga` | ヤンマガWeb | 作品ページのHTMLを `html.parser` で解析 |

**新サイトを足すときは `SOURCES` に関数を1つ増やすだけでよい。** 関数は feed 設定を受け取り、
`{key, title, link, author, thumbnail, published_at}` を持つ辞書のリストを**古い順**で返す。
`key` は重複しない識別子（RSSならguid、他はエピソードIDやパス）。

`comicwalker` と `yanmaga` は公式APIではなくサイト内部の実装に依存しているため、
サイト改修で壊れうる。**1話も取れなかった場合は例外を投げる**こと。黙って空リストを返すと
「新着なし」と区別がつかず、更新を見逃す。

### 状態管理

`seen.json` が唯一の状態で、Actions が毎回コミットして永続化する。

```
{ "feeds": { "<feed id>": ["通知済みのkey", ...] },
  "failures": { "<feed id>": {count, since, notified_at, reason} },
  "heartbeat": "YYYY-MM-DD" }
```

- **初回登録は通知しない。** `feeds` にIDが無い作品は、既存話を全部既読にするだけで投稿しない。
  過去話が一斉に流れるのを防ぐため。通知は次の新着から始まる。
- 既読リストは取得できた話だけで作り直すので、一覧から落ちた古い話は自然に消える。
- 既読への追加は**投稿が成功した話ごと**に行う。途中で Discord が落ちても投稿済みの話を二重通知しない。
- `main()` は `finally` で必ず `save_state()` する。ワークフロー側も保存ステップを `if: always()` にしてある。
- `heartbeat` は20日ごとに更新される。新着が無くてもコミットが発生し、
  **60日間リポジトリに変更が無いとスケジュール実行が自動停止する**GitHubの仕様を回避する。消さないこと。

### 失敗の扱い

ユーザーはログを見ない。異常は必ず Discord に出す。ただし鳴らしすぎると無視されるので抑制する。

- 失敗した時点で ⚠️ を通知し、直らない場合は `FAILURE_RENOTIFY_HOURS`（24時間）おきに1回だけ再通知する。
- 復旧したら ✅ を通知し、止まっていた間の新着を続けて投稿する。
- 1作品が失敗しても他の作品の処理は続行する。`main()` は個別の失敗では 0 を返す。
- `check.py` 自体の異常終了や push 失敗はスクリプトから通知できないため、
  ワークフローの `if: failure()` ステップが 🚨 を通知する。**この2層構えを崩さない。**

`FAILURE_NOTIFY_AFTER`（現在1）はチェック頻度とセットで決まる。**cron を変えるときは必ず見直す。**
1日1回のまま閾値を2にすると、失敗に気づくまで2日かかる。

## ワークフローを触るときの注意

- `actions/checkout` の `persist-credentials`（既定 true）に依存して `seen.json` を push している。
  バージョンを上げるときはこの既定値が変わっていないか確認する。
- `permissions: contents: write` が必要。
- 実行時刻はUTC指定。JSTから9時間引く。GitHubのcronは混雑時に数分〜十数分遅れる。
