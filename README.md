# web_comic

Web漫画の更新を Discord に通知する。GitHub Actions で定期実行するため、PC を起動していなくても動く。

- 外部ライブラリ不要（Python 標準ライブラリのみ）
- 監視作品数に上限なし
- パブリックリポジトリなら GitHub Actions は無料・無制限

## 仕組み

```
GitHub Actions (1日3回)
  └ feeds.json の各RSSを取得
      └ seen.json に無い話があれば Discord Webhook に投稿
          └ seen.json をコミットして既読状態を保存
```

## セットアップ

### 1. Discord の Webhook URL を用意する

通知したいチャンネルの `設定（歯車）` → `連携サービス` → `ウェブフックを作成` → `ウェブフックURLをコピー`

### 2. GitHub にリポジトリを作って push する

### 3. Webhook URL を Secrets に登録する

リポジトリの `Settings` → `Secrets and variables` → `Actions` → `New repository secret`

- Name: `DISCORD_WEBHOOK_URL`
- Secret: コピーした Webhook URL

### 4. 動作確認

`Actions` タブ → `マンガ更新チェック` → `Run workflow` → **test_post にチェックを入れて**実行。
最新話が1件 Discord に届けば成功。

## 監視作品を追加する

`feeds.json` に追記して push するだけ。サイトの種類に応じて `type` を選ぶ。

### RSS があるサイト（`type` 省略）

```json
{
  "id": "一意なID（英数字。あとから変えない）",
  "name": "作品名 / 作者名",
  "url": "RSSのURL",
  "site": "サイト名"
}
```

RSS の URL は、作品の話ページのソースから `application/rss+xml` を検索すると見つかる。
GigaViewer 採用サイト（ジャンプ+、となりのヤングジャンプ、くらげバンチ、COMIC Y-OURS など）は
`https://<サイト>/rss/series/<シリーズID>` の形式で統一されている。

ジャンプ+ には `?free_only=1` を付けた無料話限定フィードもある。

### カドコミ / コミックウォーカー（`type: comicwalker`）

RSS が無いため、サイト自身が使っている公開 JSON API を読む。

```json
{
  "id": "一意なID",
  "type": "comicwalker",
  "name": "作品名 / 作者名",
  "workCode": "KC_020003_S",
  "site": "カドコミ"
}
```

`workCode` は作品ページの URL `https://comic-walker.com/detail/<workCode>` から取れる。

### ヤンマガWeb（`type: yanmaga`）

RSS も API も無いため、作品ページの HTML から話一覧を取り出す。

```json
{
  "id": "一意なID",
  "type": "yanmaga",
  "name": "作品名 / 作者名",
  "comicCode": "GLITCH_WITCH",
  "author": "作者名",
  "site": "ヤンマガWeb"
}
```

`comicCode` は作品ページの URL `https://yanmaga.jp/comics/<comicCode>` から取れる。
作者名はページから取らず `author` に手で書く（HTML構造への依存を減らすため）。

一覧は新着順の1ページ目（10話程度）だけを見る。更新検知には十分。

### 対応していないサイトの場合

`check.py` の `SOURCES` に取得関数を1つ足せば対応できる。関数は
`{key, title, link, author, thumbnail, published_at}` を持つ辞書のリストを、
**古い順**で返せばよい。

## 実行タイミングを変える

`.github/workflows/check.yml` の `cron` を編集する。**UTC 指定**なので JST から9時間引く。

| やりたいこと | cron |
|---|---|
| JST 12:15 / 18:15 / 24:15（現在の設定） | `15 3,9,15 * * *` |
| 毎日 JST 12:15 だけ | `15 3 * * *` |
| 1時間おき | `15 * * * *` |

GitHub の cron は混雑時に数分〜十数分遅れる。分刻みの精度は期待しない。

## 手元で試す

```powershell
# 投稿せずに動作だけ確認する
$env:DRY_RUN = "1"; python check.py

# 実際に投稿してみる
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
$env:TEST_POST = "1"; python check.py
```

## ファイル

| ファイル | 役割 |
|---|---|
| `feeds.json` | 監視対象の一覧。編集するのは基本ここだけ |
| `check.py` | 本体。RSS取得 → 差分検出 → Discord投稿 |
| `seen.json` | 通知済みエピソードの記録。自動生成・自動更新 |
| `.github/workflows/check.yml` | 実行スケジュールの定義 |

## 取得に失敗したときの通知

ログを見なくても気づけるよう、失敗も Discord に流れる。ただし通知が鬱陶しくならないよう抑制している。

| タイミング | 動作 |
|---|---|
| 1回目の失敗 | 通知しない（一時的な通信エラーで騒がないため） |
| **2回連続で失敗** | ⚠️ 失敗を通知。原因のエラーメッセージ付き |
| 3回目以降も失敗 | 直るまで **24時間おきに1回だけ**再通知 |
| **復旧したとき** | ✅ 復旧を通知。止まっていた間の新着が続けて流れる |

`check.py` 自体が異常終了した場合や `seen.json` の保存に失敗した場合は、
スクリプトからは通知できないのでワークフロー側で拾って 🚨 を通知する（実行ログへのリンク付き）。

失敗の記録は `seen.json` の `failures` に入る。1作品が失敗しても他の作品の通知は止まらない。

**この仕組みでも気づけないケース:** Discord の Webhook 自体が無効になった場合と、
スケジュール実行が止まった場合。前者は GitHub からの失敗メールが、
後者は GitHub からの「ワークフローを無効化した」メールが唯一の手がかりになる。

## 備考

- **初回実行では通知が飛ばない。** 過去話の一斉通知を防ぐため、新規フィードは既存話をすべて既読として記録する。次回以降の新着から通知される。
- 1回あたりの投稿は1作品5件までに制限している（フィード側の事故による連投防止）。
- スケジュール実行は60日間リポジトリに変更がないと自動停止する。これを防ぐため、新着が無くても20日ごとに `seen.json` の `heartbeat` を更新してコミットする。
