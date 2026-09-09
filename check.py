#!/usr/bin/env python3
"""Web漫画の更新をチェックして、新着があれば Discord に通知する。

外部ライブラリは使わない（標準ライブラリのみ）。

環境変数:
  DISCORD_WEBHOOK_URL  必須。Discord のウェブフック URL。
  TEST_POST=1          新着の有無にかかわらず、各フィードの最新1話を投稿する（疎通確認用）。
  DRY_RUN=1            Discord には投稿せず、投稿内容を標準出力に表示するだけ。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent
FEEDS_PATH = ROOT / "feeds.json"
SEEN_PATH = ROOT / "seen.json"

USER_AGENT = "web-comic-notifier/1.0"
EMBED_COLOR = 0x5865F2
ERROR_COLOR = 0xE74C3C
RECOVERY_COLOR = 0x57F287
POST_INTERVAL = 1.0        # Discord のレート制限対策（秒）
MAX_POSTS_PER_FEED = 5     # 1フィードあたりの1回の投稿上限（事故時の連投防止）
HEARTBEAT_DAYS = 20        # スケジュールの自動停止を防ぐため、この間隔で必ず1回コミットする
FAILURE_NOTIFY_AFTER = 2   # 連続でこの回数失敗したら通知する（一時的な通信エラーで騒がないため）
FAILURE_RENOTIFY_HOURS = 24  # 直らないまま続く場合の再通知間隔
JST = timezone(timedelta(hours=9))


def log(message):
    print(message, flush=True)


def fetch(url, attempts=3):
    """URL を取得してバイト列を返す。一時的な失敗はリトライする。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except Exception as error:
            if attempt == attempts:
                raise
            wait = 2 ** attempt
            log(f"    取得失敗（{attempt}/{attempts}）: {error} -> {wait}秒後に再試行")
            time.sleep(wait)


def text_of(element, tag):
    child = element.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def parse_rss(raw):
    """RSS 2.0 をパースして (チャンネル名, エピソードのリスト) を返す。古い順。"""
    channel = ElementTree.fromstring(raw).find("channel")
    if channel is None:
        raise ValueError("RSS の channel 要素が見つからない")

    episodes = []
    for item in channel.findall("item"):
        link = text_of(item, "link")
        enclosure = item.find("enclosure")
        published = text_of(item, "pubDate")
        try:
            published_at = parsedate_to_datetime(published) if published else None
        except (TypeError, ValueError):
            published_at = None

        episodes.append({
            # guid が無いサイトもあるので link をフォールバックにする
            "key": text_of(item, "guid") or link,
            "title": text_of(item, "title") or "（無題）",
            "link": link,
            "author": text_of(item, "author"),
            "thumbnail": enclosure.get("url", "") if enclosure is not None else "",
            "published_at": published_at,
        })

    # RSS は新しい順で並ぶので、通知は古い順になるよう反転する
    episodes.reverse()
    return text_of(channel, "title"), episodes


def rss_episodes(feed):
    """RSS/Atom を配信しているサイト（GigaViewer 系など）。"""
    _, episodes = parse_rss(fetch(feed["url"]))
    return episodes


COMICWALKER_API = "https://comic-walker.com/api/contents/details/work?workCode={}"
COMICWALKER_EPISODE = "https://comic-walker.com/detail/{}/episodes/{}"


def comicwalker_episodes(feed):
    """カドコミ（コミックウォーカー）は RSS が無いので、サイトが使う公開JSON APIを読む。"""
    work_code = feed["workCode"]
    data = json.loads(fetch(COMICWALKER_API.format(work_code)))
    authors = "・".join(
        author.get("name", "") for author in data["work"].get("authors", [])
    )

    episodes = []
    for item in data["latestEpisodes"]["result"]:
        if not item.get("isActive", True):
            continue  # 配信が終了した話は通知しない
        try:
            published_at = datetime.fromisoformat(
                item["updateDate"].replace("Z", "+00:00")
            )
        except (KeyError, ValueError, AttributeError):
            published_at = None

        episodes.append({
            "key": item["code"],
            "title": item.get("title") or "（無題）",
            "link": COMICWALKER_EPISODE.format(work_code, item["code"]),
            "author": authors,
            "thumbnail": item.get("thumbnail", ""),
            "published_at": published_at,
        })

    # 通知は古い順に出す
    episodes.sort(key=lambda e: e["published_at"] or datetime.min.replace(tzinfo=timezone.utc))
    return episodes


YANMAGA_SERIES = "https://yanmaga.jp/comics/{}?sort=newer"
YANMAGA_ORIGIN = "https://yanmaga.jp"


class YanmagaEpisodeParser(HTMLParser):
    """ヤンマガWeb の作品ページから li.mod-episode-item を拾う。

    話一覧は HTML に直接埋まっていて、各 li が
    data-episode-title / data-original-url を持つ。
    公開日は中の time.mod-episode-date、サムネは最初の img。
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.episodes = []
        self._current = None
        self._depth = 0
        self._in_date = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()

        if tag == "li" and "mod-episode-item" in classes:
            self._current = {
                "title": (attrs.get("data-episode-title") or "").strip(),
                "path": attrs.get("data-original-url") or "",
                "thumbnail": "",
                "date": "",
            }
            self._depth = 1
            return

        if self._current is None:
            return
        if tag == "li":
            self._depth += 1
        elif tag == "img" and not self._current["thumbnail"]:
            self._current["thumbnail"] = attrs.get("src") or ""
        elif tag == "time" and "mod-episode-date" in classes:
            self._in_date = True

    def handle_endtag(self, tag):
        if self._current is None:
            return
        if tag == "time":
            self._in_date = False
        elif tag == "li":
            self._depth -= 1
            if self._depth == 0:
                self.episodes.append(self._current)
                self._current = None

    def handle_data(self, data):
        if self._in_date and self._current is not None:
            self._current["date"] += data.strip()


def yanmaga_episodes(feed):
    """ヤンマガWeb も RSS が無いので、作品ページの HTML から話一覧を取り出す。"""
    parser = YanmagaEpisodeParser()
    parser.feed(fetch(YANMAGA_SERIES.format(feed["comicCode"])).decode("utf-8", "replace"))

    episodes = []
    for item in parser.episodes:
        if not item["path"]:
            continue
        try:
            published_at = datetime.strptime(item["date"], "%Y/%m/%d").replace(tzinfo=JST)
        except ValueError:
            published_at = None

        episodes.append({
            "key": item["path"],
            "title": item["title"] or "（無題）",
            "link": YANMAGA_ORIGIN + item["path"],
            "author": feed.get("author", ""),
            # サムネは幅指定のクエリが付くので、そのまま使う
            "thumbnail": item["thumbnail"],
            "published_at": published_at,
        })

    if not episodes:
        raise ValueError("話一覧が取れなかった（ページ構造が変わった可能性）")

    # sort=newer で取っているので、通知用に古い順へ反転する
    episodes.reverse()
    return episodes


SOURCES = {
    "rss": rss_episodes,
    "comicwalker": comicwalker_episodes,
    "yanmaga": yanmaga_episodes,
}


def load_episodes(feed):
    """feeds.json の type に応じた取得処理を選ぶ。type 省略時は rss。"""
    source = feed.get("type", "rss")
    if source not in SOURCES:
        raise ValueError(f"未知の type: {source}（使えるのは {', '.join(SOURCES)}）")
    return SOURCES[source](feed)


def build_payload(feed, episode):
    embed = {
        "title": episode["title"],
        "url": episode["link"],
        "color": EMBED_COLOR,
        "author": {"name": feed["name"]},
        "footer": {"text": feed.get("site", "")},
    }
    if episode["thumbnail"]:
        embed["thumbnail"] = {"url": episode["thumbnail"]}
    if episode["published_at"]:
        embed["timestamp"] = episode["published_at"].isoformat()
    if episode["author"]:
        embed["fields"] = [{"name": "作者", "value": episode["author"], "inline": True}]

    return {
        "username": "マンガ更新",
        "content": f"**{feed['name']}** が更新されました",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }


def source_hint(feed):
    """どこを見に行って失敗したのかが分かる文字列。"""
    return feed.get("url") or feed.get("workCode") or feed.get("comicCode") or "(不明)"


def jst_text(iso_text):
    try:
        return datetime.fromisoformat(iso_text).astimezone(JST).strftime("%Y/%m/%d %H:%M")
    except (TypeError, ValueError):
        return "不明"


def build_error_payload(feed, entry):
    return {
        "username": "マンガ更新",
        "content": f"⚠️ **{feed['name']}** の更新チェックに失敗しています",
        "embeds": [{
            "title": "更新チェックに失敗",
            "description": (
                "サイトの構造変更・アクセス制限・一時的な障害などが考えられる。\n"
                "**この作品の更新を見逃している可能性があるので、手動で確認を。**\n"
                f"```\n{entry['reason']}\n```"
            ),
            "color": ERROR_COLOR,
            "author": {"name": feed["name"]},
            "footer": {"text": feed.get("site", "")},
            "fields": [
                {"name": "連続失敗", "value": f"{entry['count']} 回", "inline": True},
                {"name": "最初の失敗", "value": jst_text(entry["since"]), "inline": True},
                {"name": "監視元", "value": source_hint(feed), "inline": False},
            ],
        }],
        "allowed_mentions": {"parse": []},
    }


def build_recovery_payload(feed, entry):
    return {
        "username": "マンガ更新",
        "content": f"✅ **{feed['name']}** の更新チェックが復旧しました",
        "embeds": [{
            "title": "復旧",
            "description": "止まっていた間に公開された話があれば、続けて通知される。",
            "color": RECOVERY_COLOR,
            "author": {"name": feed["name"]},
            "footer": {"text": feed.get("site", "")},
            "fields": [
                {"name": "失敗していた期間", "value": f"{jst_text(entry['since'])} 〜", "inline": False},
            ],
        }],
        "allowed_mentions": {"parse": []},
    }


def post_to_discord(webhook_url, payload, attempts=4):
    """Discord に投稿する。429 が返ったら retry_after ぶん待って再試行する。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30):
                return
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:300]
            if error.code == 429 and attempt < attempts:
                try:
                    wait = float(json.loads(detail).get("retry_after", 5))
                except (ValueError, AttributeError, TypeError):
                    wait = 5.0
                log(f"    レート制限。{wait:.1f}秒待機して再試行")
                time.sleep(wait + 0.5)
                continue
            raise RuntimeError(f"Discord への投稿に失敗 (HTTP {error.code}): {detail}") from error


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        log(f"警告: {path.name} を読めなかったので初期化する: {error}")
        return default


def save_state(state):
    SEEN_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def touch_heartbeat(state):
    """スケジュール実行は60日間リポジトリに動きがないと自動停止する。
    新着が無い期間が続いても差分が生まれるようにして、それを防ぐ。"""
    today = datetime.now(JST).date()
    try:
        elapsed = (today - datetime.fromisoformat(state.get("heartbeat", "")).date()).days
    except ValueError:
        elapsed = HEARTBEAT_DAYS
    if elapsed >= HEARTBEAT_DAYS:
        state["heartbeat"] = today.isoformat()
        log(f"heartbeat を更新: {state['heartbeat']}")


def record_failure(state, feed, error):
    """失敗を記録し、いま通知すべきなら失敗の内容を返す。不要なら None。

    一時的な通信エラーで騒がないよう連続 FAILURE_NOTIFY_AFTER 回までは黙り、
    直らない場合も FAILURE_RENOTIFY_HOURS おきに1回しか通知しない。
    """
    now = datetime.now(timezone.utc)
    entry = state["failures"].get(feed["id"], {})
    entry["count"] = entry.get("count", 0) + 1
    entry.setdefault("since", now.isoformat())
    entry["reason"] = f"{type(error).__name__}: {error}"[:400]
    state["failures"][feed["id"]] = entry

    if entry["count"] < FAILURE_NOTIFY_AFTER:
        log(f"  （連続 {entry['count']} 回目。{FAILURE_NOTIFY_AFTER} 回で通知する）")
        return None

    notified_at = entry.get("notified_at")
    if notified_at:
        try:
            elapsed = now - datetime.fromisoformat(notified_at)
        except ValueError:
            elapsed = timedelta(days=365)
        if elapsed < timedelta(hours=FAILURE_RENOTIFY_HOURS):
            log(f"  （通知済み。次の再通知まで {FAILURE_RENOTIFY_HOURS} 時間おき）")
            return None

    entry["notified_at"] = now.isoformat()
    return entry


def clear_failure(state, feed):
    """失敗状態を解除し、復旧を通知すべきならその内容を返す。

    通知するほど失敗が続いていなかった場合は、黙って消すだけにする。
    """
    entry = state["failures"].pop(feed["id"], None)
    if entry and entry.get("notified_at"):
        return entry
    return None


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    test_post = os.environ.get("TEST_POST") == "1"
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if not webhook_url and not dry_run:
        log("エラー: 環境変数 DISCORD_WEBHOOK_URL が設定されていない")
        return 1

    feeds = load_json(FEEDS_PATH, {}).get("feeds", [])
    if not feeds:
        log("エラー: feeds.json に監視対象が無い")
        return 1

    state = load_json(SEEN_PATH, {})
    state.setdefault("feeds", {})
    state.setdefault("failures", {})

    posted = 0
    failed = []

    def send(payload, label):
        """Discord へ1件送る。DRY_RUN のときは出力するだけ。"""
        nonlocal posted
        if dry_run:
            log(f"  [DRY_RUN] {label}")
        else:
            post_to_discord(webhook_url, payload)
            log(f"  {label}")
            time.sleep(POST_INTERVAL)
        posted += 1

    try:
        for feed in feeds:
            feed_id = feed["id"]
            log(f"[{feed['name']}]")

            try:
                episodes = load_episodes(feed)
            except Exception as error:
                log(f"  取得/解析に失敗: {error}")
                failed.append(feed["name"])
                entry = record_failure(state, feed, error)
                if entry:
                    send(build_error_payload(feed, entry), f"失敗を通知（連続 {entry['count']} 回）")
                continue

            recovered = clear_failure(state, feed)
            if recovered:
                send(build_recovery_payload(feed, recovered), "復旧を通知")

            log(f"  {len(episodes)} 話を取得")
            first_run = feed_id not in state["feeds"]
            known = set(state["feeds"].get(feed_id, []))
            new_episodes = [episode for episode in episodes if episode["key"] not in known]

            if first_run:
                # 初回は既存話をすべて既読にするだけ。過去話の一斉通知を防ぐ。
                log(f"  初回登録。{len(episodes)} 話を既読として記録（通知なし）")
                state["feeds"][feed_id] = [episode["key"] for episode in episodes]
                new_episodes = []

            if test_post and not new_episodes and episodes:
                new_episodes = episodes[-1:]
                log("  TEST_POST=1 のため最新1話を投稿する")

            if len(new_episodes) > MAX_POSTS_PER_FEED:
                log(f"  新着 {len(new_episodes)} 件は多いため直近 {MAX_POSTS_PER_FEED} 件のみ投稿")
                new_episodes = new_episodes[-MAX_POSTS_PER_FEED:]

            if not new_episodes and not first_run:
                log("  新着なし")

            # 一覧から消えた話を落としつつ、通知が済んだ話だけを既読にしていく。
            # 途中で Discord 側が落ちても、投稿済みの話を二重通知しない。
            seen_now = [episode["key"] for episode in episodes if episode["key"] in known]
            for episode in new_episodes:
                send(build_payload(feed, episode), f"投稿: {episode['title']} {episode['link']}")
                if episode["key"] not in seen_now:  # TEST_POST での再投稿は重複させない
                    seen_now.append(episode["key"])
            if not first_run:
                state["feeds"][feed_id] = seen_now
    finally:
        # 途中で落ちても、そこまでの既読と失敗状態は必ず残す
        touch_heartbeat(state)
        save_state(state)

    log(f"完了: {posted} 件送信 / {len(failed)} 件失敗")
    if failed:
        log("失敗: " + ", ".join(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
