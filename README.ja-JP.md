# mei-lab-orchestral-crawler

[简体中文](README.zh-CN.md) ｜ [English](README.md) ｜ 日本語

実験的なウェブ収集エンジンです。1 回の実行で一連の URL を
「スニッフィング → ルーティング → 取得 → 抽出 → 重複排除 → 保存」の順に処理し、
成果物をローカルディスクに書き出します。

設計上の判断は [技術ホワイトペーパー](docs/WHITEPAPER.md) と
[アーキテクチャ](docs/ARCHITECTURE.md) にまとめています。

CLI + 組み込み可能なライブラリ · Python 3.12 以上 · v0.1.0 · Windows / Linux / macOS

---

## 概要

| | |
|---|---|
| エンジン | 32 モジュール、10,119 行 |
| テスト | 10 ファイル、123 テスト関数、178 ケース、すべてオフライン |
| チャネル | HTTP(curl_cffi、ブラウザ相当の TLS フィンガープリント)とブラウザ(ワーカーごとに独立した Chromium) |
| 抽出 | HTML(テキスト密度)、JSON API、JSON-LD、`__NEXT_DATA__`、`window.__NUXT__`、PDF |
| メディア | 汎用 yt-dlp チャネル、m3u8 + AES-128、5 プラットフォームのリゾルバ |
| 対検出 | 21 台の整合性の取れたデバイスプロファイル、フィンガープリント注入、ネットワークガード、挙動シミュレーション |
| キュー | SQLite WAL、リース + コンペア・アンド・スワップ、冪等なエンキュー、再開可能 |
| 安全性 | ホップ単位の SSRF ゲート(敵対的サンプル 26 件の回帰テスト)、3 層の秘匿化 |
| サイトルール | 宣言的 YAML。サイト追加にコード変更は不要 |
| リリースゲート | 6 項目。静的解析のベースラインは下降のみ許可 |

## 取得モデル

エンジン全体は 1 つの原則に沿っています。**使える中で最も安いチャネルを使う**。

URL はまずオフラインの静的スニッフィングを通り、おおよその内容と必要なコストを見積もります。
静的 HTML に見えるなら HTTP チャネル、パスパターンや内容から JS レンダリングが必要と
見えるならブラウザチャネルに振り分けます。スニッフィングは安い方向への誤りを許容します。
HTTP チャネルが返したのがシェルページで、抽出した本文がほぼ空だった場合、タスクは
キューに戻され、ブラウザチャネルで一度だけ再試行されます。

`https://quotes.toscrape.com/js/` での実測:

| チャネル | 抽出された本文 |
|---|---|
| HTTP | 29 文字(ナビゲーションのみ) |
| ブラウザ(エスカレーション後) | 1,429 文字 |

この再試行は `--max` のページ予算を消費しません。同じタスクが別チャネルを試すだけで、
新しいページではないためです。

## アーキテクチャ

```
   シード URL
       │
       ▼
 ┌───────────────┐  取得(リース + CAS)  ┌──────────────────┐
 │ SQLite キュー  │ ───────────────────► │  LayeredRouter   │
 │ タスク / メディア│ ◄─────────────────── │  (オフライン判定) │
 └───────────────┘  結果 / 再エンキュー    └────┬────────┬────┘
                                               │        │
                                  HTTP ワーカー │        │  ブラウザワーカー
                                  (curl_cffi)  ▼        ▼  (独立 Chromium)
                                        ┌─────────────────────────┐
                                        │   ExtractionPipeline    │
                                        │ HTML / JSON / PDF / メディア│
                                        └───────────┬─────────────┘
                                                    ▼
                                       ┌────────────────────────────────┐
                                       │ Vault(SQLite)→ エクスポート     │
                                       │ SHA-256 差分 · SimHash · AES    │
                                       │ JSONL · CSV · MD · XLSX · SQLite│
                                       └────────────────────────────────┘
```

モジュールの責務、1 ページの処理手順、データベースのスキーマは
[アーキテクチャ](docs/ARCHITECTURE.md) に記載しています。

## クイックスタート

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
# pip install -e ".[dev]"                              # Linux / macOS

patchright install chromium                             # ブラウザチャネル
pip install -e ".[pdf]"                                 # 任意:PDF 対応
```

ブラウザのバイナリは pip では入りません。この手順を飛ばすとブラウザチャネルが無効になり、
動的ページはデッドレターキューに落ちます。他のチャネルは通常どおり動作します。

```bash
mel crawl --url https://quotes.toscrape.com/ --max 20
mel shell                       # リンクを貼って Enter
mel stats
mel export --format markdown --output ./data
```

ライブラリとして:

```python
from meilab.engine import Engine, EngineConfig

engine = Engine(EngineConfig(output_dir="data", max_per_domain=10))
try:
    print(engine.crawl(["https://quotes.toscrape.com/"], max_tasks=20).as_dict())
finally:
    engine.close()
```

## コマンド

| コマンド | 用途 |
|---|---|
| `mel crawl --url URL [--max N]` | ページを収集。既定で robots.txt と Crawl-delay を遵守 |
| `mel shell` | 対話ウィンドウ。リンクを貼って Enter で取得 |
| `mel resume [--max N]` | キューに残った未完了タスクを続行 |
| `mel stats` | タスク / エピソード / 保管庫の統計とチャネル比率 |
| `mel export --format <fmt> --output DIR` | 書き出し:`jsonl` `csv` `markdown` `xlsx` `sqlite`(FTS5) |
| `mel media --url URL [--audio] [--cookies F]` | メディア取得:プラットフォームリゾルバ → 汎用 yt-dlp → m3u8 |
| `mel resolve --url URL` | メディアのストリームを解決のみ(ダウンロードしない) |
| `mel comments --url URL [--cookies F]` | Bilibili のコメント |
| `mel rule validate <file>` | サイトルールの検証 |
| `mel rule test <file> --url URL` | 実ページに対してルールを試行 |

## 実測値

Windows 11、Python 3.14.6、単一マシン。

```
mel crawl --url https://quotes.toscrape.com/ --max 5
  投入 21 · 配信 5 · 成功 5 · 失敗 0 · 22.5 秒

mel crawl --url https://quotes.toscrape.com/js/ --max 3
  チャネル:http 3 · browser 3
  /js/ ページ:http 0 文字 → エスカレーション → ブラウザ 1,429 文字

mel resolve --url https://www.bilibili.com/video/BV1Dvpg6BEt5/
  Bilibili ネイティブ API · 720P · mp4 · 453,914,132 バイト · 再生時間 6,972 秒
  CDN の Range 対応:206 Partial Content

fingerprint_probe.py
  10 項目 · 検出 0 件
```

フィンガープリントプローブは、本番と同じワーカー・デバイスライブラリ・注入経路を
ローカルページに対して実行し、ボット検出が一般的に拾う信号を項目ごとに報告します。
`navigator.webdriver`、`cdc_*` の残骸、`window.chrome.runtime`、`navigator.platform`、
`languages`、`hardwareConcurrency`、`maxTouchPoints`、プラグイン数、
WebGL のマスク値と未マスク値です。

## 既知の制約

以下はすべて本機での実測に基づきます。推測ではありません。

1. **Patchright 1.61.2 は `add_init_script` を実行せず、`route.fulfill` で書き換えた HTML も
   反映しません。** 同じコードは公式 Playwright では動作します。そのためフィンガープリント
   注入はページ読み込み**後**に行われ、最初のスクリプト実行前には入りません。エンジンは
   起動時にこの能力を検出し、非対応なら明示的に縮退します。
2. **Bilibili のコメントは匿名では取得できません。** 現行のエンドポイントは WBI 署名と
   ログイン状態を要求し、ブラウザ由来の cookie だけでも `-403` が返ります。
   `mel comments` はプラットフォームの理由をそのまま表示し、`--cookies` を受け付けます。
3. **抖音・小紅書・快手・NetEase Cloud は yt-dlp に委譲します。** 直リンクの署名は予告なく
   変わるため、自前の署名実装は保守負担になります。リゾルバは失敗時に汎用チャネルへ
   フォールバックし、即座に失敗扱いにはしません。
4. **GUI もインストーラもありません。** CLI とライブラリです。`mel shell` が対話的な
   フロントエンドです。
5. **`news.ycombinator.com` と `bot.sannysoft.com` はこのネットワークから到達できません**
   (TCP タイムアウト)。スモークテストは別サイトを使い、外部のフィンガープリント検査は
   ローカルプローブで代替しています。
6. **サイトルールは 3 サイト分**(Bilibili、Zhihu、テンプレート)。仕組みは完成しており、
   ルール資産はこれからです。
7. **カバレッジは報告のみで、しきい値は設けていません。** ここでは事故の回帰テストの方が
   行カバレッジより品質を語ります。
8. **300 行を超えるファイルが 7 つあります。** これらは凍結し、新しい機能は新しい
   モジュールに置いています。

## 安全上の境界

- クロール前に `robots.txt` を取得・解析し、`Disallow` と `Crawl-delay` の両方を守ります。
- 全体の最小間隔 1.5 秒にジッターを加え、ドメインごとの同時実行上限、`Retry-After` の
  バックオフとドメインクールダウンを併用します。
- 公開ページのみを対象とします。ログインウォール、ペイウォール、CAPTCHA は回避しません。
  リンク探索では `/login`、`/signup`、`/register`、`/account` を除外します。
- 外向きのリクエストは、リダイレクトの各ホップも含めて、スキームのホワイトリストと
  DNS 解決後のアドレス検査を通します。ループバック、プライベート、リンクローカル、
  クラウドメタデータのアドレスは拒否します。
- 電話番号、メールアドレス、IP、および 19 種類の機微な URL パラメータは、ディスクへ
  書き込む前に置換します。キューの URL は意図的に**秘匿化しません**。次のリクエストが
  必要とする署名を含んでいるためです。

## プロジェクト構成

```
meilab/
  core/      データモデル、SQLite キュー(状態機械、リース、CAS、マイグレーション)
  router/    スニッフィング、階層ルーティング、RSS/sitemap の投入
  workers/   プロセス基盤 + SSRF、HTTP、ブラウザ、ワーカープール
  extract/   HTML、JSON、PDF、パイプライン、サイトルール
  stealth/   デバイスプロファイル、注入、挙動、ネットワークガード、CAPTCHA
  media/     yt-dlp チャネル、m3u8、プラットフォームリゾルバ、WBI、cookie、ダウンロード
  privacy/   秘匿化
  storage/   保管庫、エクスポート、品質ゲート、可観測性
  engine.py  メインループ
  cli.py     コマンドライン
rules/sites/ 宣言的サイトルール
tools/       release_check、doc_numbers
tests/       10 ファイル、123 テスト関数
```

## 開発

```bash
python -m pytest tests/ -v        # 178 ケース、オフライン
python tools/release_check.py     # 6 項目、1 つでも失敗すればリリース不可
python tools/doc_numbers.py --check
```

リリースゲートは、バージョンの一致、作業ツリーの cleanliness、全テスト、静的解析の
ベースライン(下降のみ許可)、依存の脆弱性、秘密情報の漏洩を検査します。結論の出せない
検査は「スキップ」と記録し、「合格」とは記録しません。

## 関連プロジェクト

[kiana-crawler](https://github.com/pingguozhu071-sys/kiana-crawler) は同じ出所のもう 1 つの
収集エンジンです。Windows デスクトップ製品で、asyncio カーネル、PySide6 のウィンドウ、
622 MB のインストーラ、4 プラットフォームの直リンク解析、LLM 拡張を備えます。
本プロジェクトは同じ問題を別の道で解きます。asyncio ではなくマルチプロセス、GUI ではなく
CLI、より狭い依存集合です。

コードの共有はありません。引き継いだのはエンジニアリングの作法です。リース + CAS の
キュー意味論、ホップ単位の SSRF 検査、3 層の秘匿化、そして「同じ穴を踏まない」ための
一覧です。本プロジェクトが苦労して学んだことは
[エンジニアリング記録](docs/ENGINEERING-LESSONS.md) に残しています。

## ライセンス

MIT。[LICENSE](LICENSE) を参照してください。
