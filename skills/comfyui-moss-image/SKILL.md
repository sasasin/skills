---
name: comfyui-moss-image
description: ローカルの ComfyUI と MOSS-Image GGUF ワークフローを使い、ユーザーの指示から画像を生成する。接続先・モデル名・出力先は環境で変わるため、決め打ちせず確認してから渡す。解度やサンプラー設定の変更、すでにキューに入っている生成の追従も行う。ComfyUI で画像をつくってほしいと頼まれたときに使う。「画像生成して」「このプロンプトで絵を描いて」「ComfyUI で回して」「途中で投げた生成見せて」。
compatibility: ComfyUI が API（/prompt・/object_info・/history・/queue・/free）で到達できること。Python 3（標準ライブラリのみ）と shell の実行ができること。同梱グラフは GGUF ロード系・Flux2 VAE 系ノードを使うため、無い環境では --workflow でその環境のグラフを渡す
---

# ComfyUI MOSS-Image

ComfyUI のワークフロー 1 本（UI form の JSON）と、それを API form に変換して送信し、完了を待ち、画像を取り出すスクリプト `comfy_run.py` をまとめたもの。同梱グラフは MOSS-Image SFT (GGUF) 向けで、作者のローカル環境のモデル名に patch されている。

## 実行のしかた

スクリプトはこのスキルフォルダ内の `comfy_run.py`。インストール先はスキルをロードしたときに示されるパスなので、それをたどって絶対パスで実行する。ワークフローはスクリプトと同じ場所から見つける。

```sh
python3 "<スキルフォルダ>/comfy_run.py" --dry-run
python3 "<スキルフォルダ>/comfy_run.py" --prompt-file /path/to/prompt.txt --size 832x1248 --prefix Image
```

`--debug` の既定の出力先は実行時のカレントディレクトリなので、スキルフォルダを cwd にしない。

## 環境の値は決め打ちしない（最初に 1 回だけ確かめる）

| 決めること | 確かめ方 |
|---|---|
| ComfyUI の URL | 環境変数 `COMFY_URL` があればそれ。無ければ `http://127.0.0.1:8188` に `GET /system_stats` を投げ、応答があればそれで進む。届かなければユーザーに起動状況を確認する |
| 使えるモデルファイル名 | `GET /object_info/UnetLoaderGGUF`・`/object_info/CLIPLoader`・`/object_info/VAELoader` が返す選択肢リスト。同梱グラフは作者環境の `MOSS-Image-SFT-Q4_K_M.gguf` などを前提にしているので、その環境に無い名前は `--unet` / `--clip` / `--vae` で実際の名前に差し替える |
| 画像の保存先 | ユーザーの指示に従い `--output-dir` で渡す。既定は作者環境の `~/Pictures/ComfyUI` |
| 同じ GPU を使う推論エンジンがあるか | テキスト生成サーバなどが同じ GPU を共有しているなら `--unload-engine URL`、無いなら `--no-unload-engine`。エンジンの種類は問わない（`/v1/unload` を持つ HTTP サーバなら対象） |

`--unload-engine` は未指定だと `http://127.0.0.1:8080` を試し、存在しないか別のサービスだった場合は警告して先へ進む。

## 生成までの手順

1. `--dry-run` で変換結果を確認する。ワークフローに関わるオプション（`--size`、`--sampler` など）を変えるときは必ずここでグラフを見る。
2. ユーザーの依頼が曖昧な部分は、使うプロンプトと設定を要約して提示してから送信する。
3. 同じコマンドから `--dry-run` を外して送信する。スクリプトは完了まで待ち、画像を `--output-dir` に保存し、その後 ComfyUI の `/free` を呼んでモデルを降ろす。
4. 保存パスと、必要なら seed を報告する。すでに送信済みのジョブを追うだけなら `--follow PROMPT_ID`。

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
| GPU を譲る先 | `--unload-engine URL`（複数可）、`--no-unload-engine`（`UNLOAD_ENGINE_URL`、`UNLOAD_ENGINE_API_KEY`。旧名 `STRATA_URL`・`STRATA_API_KEY` も読む） |
| モデル保持 | `--keep-models` |

## 書き出すもの

通常の運用ではワークフローファイルを書かない。変換後の API form グラフを確かめたいときだけ `--debug` を付け、パスを省略すると実行ディレクトリの `moss_image_sft.api.json` に保存される。これは生成物で、スキルの一部ではない。

画像生成は、ユーザーの明示的な依頼に基づく行為として扱う。生成された画像も、そのとき使ったプロンプトファイルも同梱しない。
