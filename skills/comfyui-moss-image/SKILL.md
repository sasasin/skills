---
name: comfyui-moss-image
description: ローカルの ComfyUI と MOSS-Image GGUF ワークフローを使い、ユーザーの指示から画像を生成する。接続先・モデル名・出力先・GPU を譲る相手は、初回の 1 回だけ調べて env.json に保存し、以降はそれを読むだけで再調査しない。解度やサンプラー設定の変更、すでにキューに入っている生成の追従も行う。ComfyUI で画像をつくってほしいと頼まれたときに使う。「画像生成して」「このプロンプトで絵を描いて」「ComfyUI で回して」「途中で投げた生成見せて」。
compatibility: ComfyUI が API（/system_stats・/object_info・/prompt・/history・/queue・/free）で到達できること。Python 3（標準ライブラリのみ）と shell の実行ができること。同梱グラフは GGUF ロード系・Flux2 VAE 系ノードを使うため、無い環境では --workflow でその環境のグラフを渡す。環境検出の結果は ~/.local/state/comfyui-moss-image/ に書く（スキルフォルダには書かない）
metadata:
  author: sasasin
  state-dir: ${XDG_STATE_HOME:-~/.local/state}/comfyui-moss-image
---

# ComfyUI MOSS-Image

ComfyUI のワークフロー 1 本（UI form の JSON）と、それを API form に変換して送信し、完了を待ち、画像を取り出すスクリプト `comfy_run.py` をまとめたもの。同梱グラフは MOSS-Image SFT (GGUF) 向けで、作者のローカル環境のモデル名に patch されている。

## 実行のしかた

スクリプトはこのスキルフォルダ内の `comfy_run.py`。インストール先はスキルをロードしたときに示されるパスなので、それをたどって絶対パスで実行する。ワークフローはスクリプトと同じ場所から見つける。

```sh
python3 "<スキルフォルダ>/comfy_run.py" --setup     # 初回だけ（以降は実行しない）
python3 "<スキルフォルダ>/comfy_run.py" --dry-run
python3 "<スキルフォルダ>/comfy_run.py" --prompt-file /path/to/prompt.txt --size 832x1248 --prefix Image
```

`--debug` の既定の出力先は実行時のカレントディレクトリなので、スキルフォルダを cwd にしない。

## 環境の値は env.json から読む（調べていいのは 1 回だけ）

接続先・モデル名・出力先・推論エンジンは `env.json` に保存されている。パスは
`${XDG_STATE_HOME:-~/.local/state}/comfyui-moss-image/env.json`（`COMFY_STATE_DIR`・`COMFY_ENV_FILE`・`--env-file` で変更）。
解決順は **CLI 引数 > 環境変数 > env.json > スクリプトの既定値**。

`env.json` が無い・TTL（既定 1 日）切れ・同梱グラフの変更、のいずれかなら、スクリプトが自分で検出して書き直す。**エージェントが API を漁いにいく必要はない。**環境を知りたいときは `--print-env` 1 回、または `env.json` を読む（`--print-env` は検出しに行かず、無いなら「無い」と言うだけ）。

| env.json のキー | 意味 |
|---|---|
| `comfy_url` | 到達できた ComfyUI の base URL |
| `comfy` / `devices` | ComfyUI の版・OS と、見えた GPU 名（`/system_stats` の写し） |
| `models.{unet,clip,vae}.name` | 採用するモデルファイル名（`--unet` 等の既定値になる） |
| `models.*.verified` / `.candidates` | 同梱グラフの名前が実在したか、とその環境の実在ファイル一覧。`verified: false` のとき `candidates` から選ぶ |
| `engines[]` | `/v1/unload` を持つ推論エンジン（無い環境では空 or `unload_api: false`） |
| `output_dir` / `keep_models` | 検出時に指定した値。再検出でも引き継がれる |
| `fingerprint` / `ttl_seconds` | 検出結果がどの環境のものか、と再検出までの秒数 |

同じ内容で良さそうなら、`--setup` だけ先に走らせておくのも速い（`--setup` は engine を unload するため、生成を送る前に実行する）。`--no-env` で env.json を完全に無効化して従来動作に戻せる。

## 生成までの手順

1. `--print-env` で設定を見る（env.json が無くても、その実行で自動検出される）。モデルが `verified: false` なら `candidates` から選び `--unet` 等で確定させる。
2. `--dry-run` で変換結果を確認する。ワークフローに関わるオプション（`--size`、`--sampler` など）を変えるときは必ずここでグラフを見る。
3. ユーザーの依頼が曖昧な部分は、使うプロンプトと設定を要約して提示してから送信する。
4. 同じコマンドから `--dry-run` を外して送信する。スクリプトは完了まで待ち、画像を `--output-dir` に保存し、その後 ComfyUI の `/free` を呼んでモデルを降ろす。
5. 保存パスと、必要なら seed を報告する。すでに送信済みのジョブを追うだけなら `--follow PROMPT_ID`。

生成中に他プロセスと GPU を共有する環境では、ComfyUI がモデルを保持したままにすると共有側が押し出されて OOM になる（1 回の実測: 保持時 使用 29.96 GB / 空き 1.90 GB → `/free` 後 使用 17.33 GB / 空き 14.53 GB）。そのため既定では送信前に推論エンジンを unload し、完了後に `/free` を要求する。GPU を専有している環境や、モデルを降ろしてほしくないときは `--keep-models` を使う（明示的に求められたときだけ）。

## オプション一覧

| 区分 | 引数（環境変数） |
|---|---|
| 接続先 | `--comfy-url`（`COMFY_URL`） |
| 基準グラフ | `--workflow`（`COMFY_WORKFLOW`） |
| 出力先 | `--output-dir`（`COMFY_OUTPUT_DIR`） |
| モデル名 | `--unet`（`MOSS_UNET`）、`--clip`（`MOSS_CLIP`）、`--vae`（`MOSS_VAE`） |
| プロンプト | `--prompt-file`、`--negative-file` |
| 生成設定 | `--size WxH`（縦横とも 16 の倍数）、`--steps`、`--cfg`、`--sampler`、`--scheduler`、`--prefix` |
| 再現・待ち時間 | `--seed`（`SEED`）、`--timeout`（`WAIT_TIMEOUT`） |
| 追従・確認 | `--follow PID`、`--dry-run`、`--debug [PATH]` |
| 環境検出 | `--setup`、`--refresh-env`、`--print-env`、`--no-auto-setup`、`--no-env`、`--env-file PATH`、`--env-ttl N`（`COMFY_ENV_TTL`）、`COMFY_STATE_DIR` |
| GPU を譲る先 | `--unload-engine URL`（複数可）、`--no-unload-engine`（`UNLOAD_ENGINE_URL`、`UNLOAD_ENGINE_API_KEY`。旧名 `STRATA_URL`・`STRATA_API_KEY` も読む。トークンは env.json に保存しない） |
| モデル保持 | `--keep-models` / `--no-keep-models`（env.json に保存される） |

## 書き出すもの

環境検出の結果は `${XDG_STATE_HOME:-~/.local/state}/comfyui-moss-image/` に書く。

| ファイル | 中身 |
|---|---|
| `env.json` | 接続先・モデル名と候補・GPU・推論エンジン・TTL・fingerprint（schema 1） |
| `nodes.json` | `object_info` の写し。fingerprint が一致する間だけ使い、生成時の HTTP を減らす |

**スキルフォルダには何も書かない。**`npx skills update` はスキルフォルダを再生成し、`~/.agents/.skill-lock.json` はフォルダ全体のハッシュを持つので、機械別の値を同梱物に混ぜると更新で消え、更新判定も汚れる。

通常の運用ではワークフローファイルを書かない。変換後の API form グラフを確かめたいときだけ `--debug` を付け、パスを省略すると実行ディレクトリの `moss_image_sft.api.json` に保存される。これは生成物で、スキルの一部ではない。

画像生成は、ユーザーの明示的な依頼に基づく行為として扱う。生成された画像も、そのとき使ったプロンプトファイルも同梱しない。
