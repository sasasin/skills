# quick-research

Pi / Claude Code などエージェント向けの **Agent Skill**。5分程度のライトな調査レポートを
「結論 → 根拠 → 出典」の形で返すための手順（検索予算、リード表、検証チェックリスト、出力フォーマット）を定義します。

深い芋づる式調査や網羅的な deep research ではなく、
「最新情報から調べて」「〜の現状を整理して」「なぜ〜なのか業界はどう見ているか」
のような依頼に向きます。

## インストール

[skills CLI](https://github.com/vercel-labs/skills)（`npx skills`）でインストールできます。

```bash
# このリポジトリのスキルを一覧だけ確認
npx skills add sasasin/quick-research --list

# ユーザー全体（グローバル）にインストール
npx skills add sasasin/quick-research -g -y
```

対象エージェントを明示する場合:

```bash
# Pi / Codex / Cline など (.agents/skills, ~/.agents/skills に導入)
npx skills add sasasin/quick-research -g -a pi -y

# Claude Code (~/.claude/skills に導入)
npx skills add sasasin/quick-research -g -a claude-code -y
```

Pi を直接使っている場合は、スキルフォルダに手動で置いても動作します。

```bash
git clone https://github.com/sasasin/quick-research /tmp/qr
mkdir -p ~/.pi/agent/skills
cp -r /tmp/qr ~/.pi/agent/skills/quick-research
```

Pi のスキル検索対象ディレクトリは `~/.pi/agent/skills/` と `~/.agents/skills/` の両方です。
同じ `name` のスキルを両方に置かないでください。

## 使い方

インストール後、エージェントに対して

```
/skill:quick-research 日経平均の急落について5分でまとめて
```

のように呼び出すか、「5分で軽い調査レポートを」と自然言語で依頼すると
スキルが発火します。

## リポジトリ構成

```
.
├── SKILL.md      # スキル本体 (YAML frontmatter: name, description)
├── README.md
└── LICENSE       # MIT
```

`SKILL.md` をルートに置いているため、`npx skills add <owner>/<repo>` で
そのまま発見されます。スキルを増やす場合は `skills/<name>/SKILL.md` 形式にもできます。

## 更新

```bash
npx skills check      # 更新があるか確認
npx skills update     # 更新を適用
```

## ライセンス

[MIT](LICENSE)
