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

`feeds.json` に追記して push するだけ。

```json
{
  "id": "一意なID（英数字。あとから変えない）",
  "name": "作品名 / 作者名",
  "url": "RSSのURL",
  "site": "サイト名"
}
```

RSS の URL は、作品の話ページを開いてページのソースから `application/rss+xml` を検索すると見つかる。
GigaViewer 採用サイト（ジャンプ+、となりのヤングジャンプ、くらげバンチ、COMIC Y-OURS など）は
`https://<サイト>/rss/series/<シリーズID>` の形式で統一されている。

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

## 備考

- **初回実行では通知が飛ばない。** 過去話の一斉通知を防ぐため、新規フィードは既存話をすべて既読として記録する。次回以降の新着から通知される。
- 1回あたりの投稿は1作品5件までに制限している（フィード側の事故による連投防止）。
- スケジュール実行は60日間リポジトリに変更がないと自動停止する。これを防ぐため、新着が無くても20日ごとに `seen.json` の `heartbeat` を更新してコミットする。
