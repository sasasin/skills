# sasasin/skills

Personal agent skills pack. / 私専用の Agent Skills をまとめたリポジトリ。

このリポジトリ内のすべてのスキルは [MIT License](LICENSE) です（今後追加されるスキルにも同じライセンスが適用されます）。

## Included skills

| Skill | 何をするか |
|---|---|
| [quick-research](skills/quick-research/SKILL.md) | 5分程度のライトな調査レポートを「結論→根拠→出典」で返す。検索10回・本文取得4件の予算管理つき |

## Install

[skills CLI](https://github.com/vercel-labs/skills)（`npx skills`）を使います。

```bash
# このリポジトリに入っているスキルを一覧だけ確認
npx skills add sasasin/skills --list

# 対話でスキルとエージェントを選ぶ
npx skills add sasasin/skills

# 特定スキルを Pi にグローバル導入
npx skills add sasasin/skills --skill quick-research -g -a pi -y

# Claude Code にも（同じ名前で複数エージェントへ）
npx skills add sasasin/skills --skill quick-research -g -a claude-code -y

# 全部のスキルを全部のエージェントへ
npx skills add sasasin/skills --all
```

`--skill` に渡す名前はフォルダ名ではなく `SKILL.md` frontmatter の `name` です。

Pi を直接使っている場合は、スキルフォルダへ手動で置いても動きます。

```bash
git clone https://github.com/sasasin/skills /tmp/sasasin-skills
mkdir -p ~/.agents/skills
cp -r /tmp/sasasin-skills/skills/quick-research ~/.agents/skills/
```

Pi は `~/.pi/agent/skills/` と `~/.agents/skills/` の両方を読むので、同じ `name` を両方に置かないでください。

## Update / remove

```bash
npx skills check
npx skills update quick-research
npx skills remove --global quick-research
```

## Portability（Web検索・URL取得のツール名）

スキル本文はツール名を固定していません。`web_search` や `fetch_url` はその環境に存在しないことがあり、
SKILL.md 側で「能力 → よくあるツール名」の対応表を持ちます。

| 環境 | 検索 | URL取得 |
|---|---|---|
| Pi | `web_search`（拡張により `web_research` / `fetch_url`） | `web_fetch` |
| Claude Code | `WebSearch` | `WebFetch` |
| Codex CLI | `web_search`（cached / live） | —（shell の `curl` で代替） |
| Gemini CLI | `google_web_search` | `web_fetch` |
| OpenCode | `websearch` | `webfetch` |
| Cline | —（MCPで追加するのが確実） | `fetch_web_content` / `fetch_web` |
| MCP 系 | `brave_web_search`・Tavily `web_search`・Exa `web_search_exa` | `fetch` |

検索も取得も見つからない環境では、推測で「最新」を書かず、Webアクセスが無いことを伝えて止まります。

## Layout

```
.
├── LICENSE                          # MIT（リポジトリ全体のスキルに適用）
├── README.md
└── skills/
    └── quick-research/
        └── SKILL.md                 # name / description / compatibility の frontmatter
```

スキルの追加は `skills/<name>/SKILL.md` が基本。カテゴリで纏める場合は
`skills/<category>/<name>/SKILL.md`（2階層まで、3階層目まで発見されます）。
ルート直下に `SKILL.md` を置くと `skills/` 配下が shadow されるので置かないこと。

```bash
# 追加例
mkdir -p skills/new-skill
$EDITOR skills/new-skill/SKILL.md      # frontmatter の name を決める（他リポジトリとかぶらない名前で）
npx skills add . --list                # 発見されるかローカルで確認
git add skills/new-skill && git commit -m "feat: new-skill" && git push
```

SKILL.md と同じフォルダに置く参照ファイル（メモ・チェックリスト・コード）も一緒にインストールされます。

## License

MIT — see [LICENSE](LICENSE).
