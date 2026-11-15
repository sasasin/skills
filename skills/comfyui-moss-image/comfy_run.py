#!/usr/bin/env python3
"""ComfyUI の UI(graph) form ワークフローを API form に変換して実行し、出力画像を取り出す。

Python 標準ライブラリだけで動く。接続先・モデル名・出力先などの環境差は
「CLI 引数 > 環境変数 > env.json（この機械の検出結果）> このファイルの既定値」の順で解決する。

env.json は「その機械で 1 回だけ調べた結果」を書き置くファイルで、置き場は
    ${XDG_STATE_HOME:-~/.local/state}/comfyui-moss-image/env.json
    （COMFY_STATE_DIR / COMFY_ENV_FILE / --env-file で変更できる）
スキルフォルダは npx skills の update で再生成される配布物なので、機械別の値をそこへは書かない。
env.json が無い・TTL 切れ、のどちらかなら実行時に自動で検出して書き、以降はファイルを読むだけになる。

使い方:
    python3 comfy_run.py                 # 変換して送信し、完了を待って画像を取り出す
    python3 comfy_run.py --setup         # 環境を調べて env.json に保存するだけ（生成はしない）
    python3 comfy_run.py --print-env     # 解決済みの設定と env.json の鮮度を表示するだけ（送信しない）
    python3 comfy_run.py --dry-run       # 変換結果を表示するだけ（送信しない）
    python3 comfy_run.py --follow PID    # 送信済みジョブの追従だけ行う
    python3 comfy_run.py --debug [PATH]  # 変換後の API form JSON を保存して中身を確かめる（既定では書かない）
    python3 comfy_run.py --keep-models   # 生成後に ComfyUI のモデルを降ろさない

環境検出（env.json / nodes.json）:
    --setup                             推論エンジンを unload する可能性があるため、生成を送る前に行う
    --refresh-env                       実行前に強制再検出
    --no-auto-setup                     失効していても自動検出せず、既定値で進む
    --no-env                            env.json / nodes.json を一切使わない
    --keep-models / --no-keep-models    検出結果に保存され、以降の実行で引き継がれる
    --env-file PATH                     env.json の場所（既定は上のパス）
    --env-ttl N / COMFY_ENV_TTL         再検出までの秒数（既定 86400。0 にすると毎回検出）

接続先・ファイル・環境差:
    --comfy-url URL     / COMFY_URL          ComfyUI の API base（既定 http://127.0.0.1:8188）
    --workflow PATH     / COMFY_WORKFLOW     基準にする UI form ワークフロー（既定は同梱の moss_image_sft.workflow.json）
    --output-dir PATH   / COMFY_OUTPUT_DIR   生成画像の保存先（既定 ~/Pictures/ComfyUI）
    --unet NAME         / MOSS_UNET          ┐ この ComfyUI に実在するモデルファイル名。
    --clip NAME         / MOSS_CLIP          ├ 同梱グラフは作者のモデル名に patch されているので、
    --vae NAME          / MOSS_VAE           ┘ 他の環境では必ず自環境の名前を渡す
                                         （env.json の models[*].candidates に実在する名前が並ぶ）
    --seed N            / SEED               再現用 seed
    --timeout N         / WAIT_TIMEOUT       完了待ちの上限秒数
    --unload-engine URL                     GPU を共有している推論エンジンの base URL（複数指定可）
    --no-unload-engine                      エンジン解放を一切しない
    UNLOAD_ENGINE_URL   旧 STRATA_URL        カンマ区切りで複数
    UNLOAD_ENGINE_API_KEY / 旧 STRATA_API_KEY  Bearer トークン（トークンは env.json に書かない）

既定では生成後に ComfyUI へ /free を要求してモデルを降ろす。同じ GPU を使う推論
エンジンがある環境では、これをしないと ComfyUI がモデルを保持したままになって
エンジン側が押し出され OOM になる（1 回の実測: 保持時 使用 29.96 GB / 空き 1.90 GB
→ /free 後 使用 17.33 GB / 空き 14.53 GB）。GPU を専有している環境では --keep-models。

プロンプトや解度の差し替え（基準ワークフローのファイルは壊さず、CLI からのみ上書き）:
    python3 comfy_run.py --prompt-file p.txt        # positive を差し替え
    python3 comfy_run.py --negative-file n.txt      # negative を差し替え（ConditioningZeroOut を実ノードに置換）
    python3 comfy_run.py --size 832x1248            # 解度（16 の倍数）
    python3 comfy_run.py --steps 30 --cfg 2.5 --sampler euler --scheduler simple
    python3 comfy_run.py --prefix IMG               # SaveImage の filename_prefix
"""
import difflib
import errno
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- 環境差の既定値（作者のローカル向け。引数か環境変数で上書きする） -----------
HERE = os.path.dirname(os.path.abspath(__file__))
COMFY_DEFAULT = "http://127.0.0.1:8188"
WORKFLOW_DEFAULT = os.path.join(HERE, "moss_image_sft.workflow.json")
OUTPUT_DEFAULT = "~/Pictures/ComfyUI"
DEBUG_NAME = "moss_image_sft.api.json"      # --debug にパスを渡さないときの名前（実行ディレクトリに書く）
SEED_DEFAULT = "20261114"
WAIT_DEFAULT = "1500"
ENGINE_DEFAULT = "http://127.0.0.1:8080"    # 対象が無い環境では応答を見て skip する

# 環境検出結果の置き場。スキルフォルダ（配布物）ではなく XDG の state を使う。
SKILL_ID = "comfyui-moss-image"
ENV_NAME = "env.json"
NODES_NAME = "nodes.json"
ENV_SCHEMA = 1
TTL_DEFAULT = "86400"                        # 1 日たったら再検出

# 実行時に configure() で埋める。既定値に頼ったまま動かない環境を減らすため、
# ここでは解決しない。
BASE = None
WF_IN = None
API_OUT = None
OUT_DIR = None
WAIT_TIMEOUT = None
SEED = None
MODELS = {}
ENGINES = []

# env.json（この機械の検出結果）と、その読み込み状態
ENV = None
ENV_PATH = None
ENV_NOTE = ""
ENV_REASON = ""
NODE_CACHE = {}     # class_type -> object_info（このプロセス内）
NODE_DISK = {}      # nodes.json から読んだ分（fingerprint が一致したときだけ使う）
NODE_DIRTY = False

# ComfyUI の UI widget でしか使われない sentinel 値。object_info の選択肢に
# 存在せず、widgets_values の個数だけがノード数より多い場合に捨てる。
UI_ONLY_SENTINELS = {"randomize", "fixed", "increment", "decrement"}

SEED = int(os.environ.get("SEED", "20261114"))

# --unet/--clip/--vae でローダノードを探すための照合キー。
# class_type を小文字・区切り無しにした文字列に対して部分一致させる（VAEDecode 等に誤マッチしない）。
LOADER_MATCHES = {
    "unet": ("unetloader", "loaddiffusion", "diffusionloader"),
    "clip": ("cliploader", "loadclip", "textencoderloader"),
    "vae": ("vaeloader", "loadvae"),
}

# node_id: (new_type_or_None, new_widgets_values)
# 同梱のワークフローは作者の ComfyUI に合わせた patch。モデル名は --unet/--clip/--vae で上書きできる。
PATCHES = {
    1: (None, ["MOSS-Image-SFT-Q4_K_M.gguf"]),
    2: ("CLIPLoaderGGUFDynamicVRAM", ["Qwen3.5-9B-Q4_K_M.gguf", "moss_image"]),
    17: (None, ["flux2-vae_small.safetensors"]),
}

# ノード説明文は実行に不要なので捨てる
SKIP_TYPES = {"MarkdownNote"}

# 初回基準は 1024x1024 に固定する。ResolutionSelector は孤立するうえ
# widgets_values の個数が object_info より 1 個多く（UI 専用 widget がある）、
# ノードごと捨てて EmptyFlux2LatentImage の直値（1024x1024）を使う。
DROP_IDS = {13}

# 生成完了待ち（秒）。前回は 3 点ロード込みで約 2 分だった。
WAIT_TIMEOUT = int(os.environ.get("WAIT_TIMEOUT", "1500"))


class ComfyError(RuntimeError):
    pass


def api(path, data=None, *, base=None, headers=None):
    url = (base or BASE) + path
    body = None
    req = urllib.request.Request(url)
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, body, timeout=120) as res:
            raw = res.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        try:
            detail = json.dumps(json.loads(detail), ensure_ascii=False, indent=2)
        except Exception:
            pass
        raise ComfyError(f"HTTP {e.code} {path}\n{detail}") from e
    except urllib.error.URLError as e:
        raise ComfyError(f"接続不能 {url}: {e.reason}") from e
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ComfyError(f"JSON でない応答 {path}: {raw[:400]}")


def node_spec(class_type):
    """object_info のノード定義を取ってくる（nodes.json にキャッシュ済みなら HTTP を省く）。"""
    global NODE_DIRTY
    if class_type not in NODE_CACHE:
        if class_type in NODE_DISK:
            NODE_CACHE[class_type] = NODE_DISK[class_type]
            return NODE_CACHE[class_type]
        d = api(f"/object_info/{class_type}")
        if class_type not in d:
            raise ComfyError(f"ノード定義がありません: {class_type}")
        NODE_CACHE[class_type] = d[class_type]
        NODE_DISK[class_type] = d[class_type]
        NODE_DIRTY = True
    return NODE_CACHE[class_type]


def has_node(class_type):
    """その ComfyUI にノード定義が存在するか（カスタムノードの有無は環境ごとに違う）。"""
    try:
        node_spec(class_type)
        return True
    except ComfyError as e:
        if str(e).startswith("接続不能"):
            raise      # ComfyUI に届かないのを「ノードが無い」と誤解させない
        return False


def widget_names(spec):
    """object_info に現れる「値を受け取るパラメータ」を順番どおり取る。

    inputs 配列に名前が出るもの（model/positive 等の接続）は widget ではない。
    hidden 等の system 値は required に現れないので自動的に除外される。
    """
    out = []
    for section in ("required", "optional"):
        for name, decl in spec["input"].get(section, {}).items():
            out.append(name)
    return out


def allowed_values(spec):
    vals = set()
    for decl in {**spec["input"].get("required", {}), **spec["input"].get("optional", {})}.values():
        if isinstance(decl, list) and decl and isinstance(decl[0], list):
            for o in decl[0]:
                if isinstance(o, str):
                    vals.add(o)
    return vals


def align_widgets(widgets, names, spec, nid, notes):
    """widgets_values を object_info のパラメータ順に並べる。

    UI 専用の control_after_generate（"randomize"/"fixed"）のように、
    object_info に存在しない値が挟まっていると全体が 1 つずつずれる。
    多いときは sentinel を除いて詰め、それでも余れば末尾を捨てる。
    足りないときは ComfyUI の default 任せにする（送信しない）。
    """
    if len(widgets) == len(names):
        return list(widgets)
    allowed = allowed_values(spec)
    kept, dropped = [], []
    if len(widgets) > len(names):
        extra = len(widgets) - len(names)
        for w in widgets:
            if len(dropped) < extra and isinstance(w, str) and w in UI_ONLY_SENTINELS and w not in allowed:
                dropped.append(w)
                continue
            kept.append(w)
        if len(kept) > len(names):
            dropped.extend(kept[len(names):])
            kept = kept[:len(names)]
        if dropped:
            notes.append(f"[{nid}] object_info に無い widget を除去: {dropped}")
        return kept
    notes.append(f"[{nid}] widget {len(names)-len(widgets)} 個不足 → ComfyUI の default に任せる")
    return list(widgets)


def convert(graph):
    links = {l[0]: l for l in graph.get("links") or []}
    skip_ids = set(DROP_IDS)
    for n in graph["nodes"]:
        if n["type"] in SKIP_TYPES:
            skip_ids.add(n["id"])
    out = {}
    notes = []
    for n in graph["nodes"]:
        nid = n["id"]
        class_type = n["type"]
        if nid in skip_ids:
            notes.append(f"[{nid}] {class_type} は除外（DROP_IDS/SKIP_TYPES）")
            continue
        new_type, new_widgets = PATCHES.get(nid, (None, None))
        if new_type:
            if not has_node(new_type):
                notes.append(f"[{nid}] {new_type} はこの ComfyUI に無いため {class_type} のまま使う")
                new_type = None
            else:
                notes.append(f"[{nid}] {class_type} → {new_type}")
        if new_type:
            class_type = new_type
        spec = node_spec(class_type)

        inputs = {}
        for conn in n.get("inputs") or []:
            link = conn.get("link")
            if link is None:
                continue
            src = links[link]
            src_id = src[1]
            if src_id in skip_ids:
                notes.append(f"[{nid}] 配線解除: {conn['name']} ← 除外ノード {src_id}")
                continue
            inputs[conn["name"]] = [str(src_id), src[2]]

        names = [nm for nm in widget_names(spec) if nm not in inputs]
        widgets = list(new_widgets) if new_widgets is not None else list(n.get("widgets_values") or [])
        widgets = align_widgets(widgets, names, spec, nid, notes)
        for name, value in zip(names, widgets):
            if value is not None:
                inputs[name] = value

        out[str(nid)] = {"class_type": class_type, "inputs": inputs,
                         "_meta": {"title": n.get("title") or class_type}}
    return out, notes


def apply_model_overrides(prompt, notes):
    """--unet/--clip/--vae で、ローダノードの最初の文字列 widget を差し替える。

    同梱グラフのモデル名は作者の環境向けなので、他の環境ではこれを渡す。
    """
    argv = sys.argv[1:]
    for role in LOADER_MATCHES:
        name = MODELS.get(role)
        if not name:
            continue
        pats = LOADER_MATCHES[role]
        hits = []
        for nid, p in prompt.items():
            ctype = p["class_type"].lower().replace("_", "")
            if not any(pat in ctype for pat in pats):
                continue
            for key in list(p["inputs"]):
                if isinstance(p["inputs"][key], str):
                    old = p["inputs"][key]
                    p["inputs"][key] = name
                    hits.append(f"[{nid}] {p['class_type']}.{key}: {old!r} → {name!r}")
                    break
        if not hits:
            raise ComfyError(f"--{role} の差し替え先が見つかりませんでした（探索キー: {'/'.join(pats)}）"
                             f"\n  その環境では別ノードの可能性があるので、自環境のワークフローを --workflow で渡してください"
                             f"\n  実在するファイル名は --print-env（または env.json の models.{role}.candidates）に出ます")
        notes.extend(hits)
    return notes


def apply_seed(prompt, seed):
    """seed / noise_seed を持つノードに固定 seed を入れる（再現基準を作るため）。"""
    touched = []
    for nid, p in prompt.items():
        for key in list(p["inputs"].keys()):
            if key in ("seed", "noise_seed"):
                p["inputs"][key] = seed
                touched.append(f"[{nid}] {key} = {seed}")
    if not touched:
        touched.append("seed を持つノードが見つかりませんでした")
    return touched


def images_of(history_record):
    found = []
    for node_out in (history_record.get("outputs") or {}).values():
        for key in ("gifs", "images", "audio"):
            for item in node_out.get(key) or []:
                found.append(item)
    return found


def queue_snapshot():
    """/queue の要素は [優先度, prompt_id, workflow, number, node_count] の配列。"""
    q = api("/queue")
    running = []
    for entry in q.get("queue_running") or []:
        if isinstance(entry, list) and len(entry) > 1:
            running.append(entry[1])
        else:
            running.append(repr(entry)[:20])
    pending = len(q.get("queue_pending") or [])
    return running, pending


def wait_done(pid, t0):
    """履歴が埋まるまで待ち、そのレコードを返す。"""
    while True:
        time.sleep(3)
        rec = api(f"/history/{pid}").get(pid)
        if rec:
            status = rec.get("status") or {}
            if status.get("status_str") == "error" or status.get("completed") is False:
                print("\n=== status: error ===")
                print(json.dumps(status, ensure_ascii=False, indent=2)[:3000])
                raise ComfyError("生成がエラーで終了しました")
            if images_of(rec) or status.get("completed"):
                return rec
        if time.time() - t0 > WAIT_TIMEOUT:
            raise TimeoutError(f"{WAIT_TIMEOUT}s 以内に完了しませんでした")
        running, pending = queue_snapshot()
        print(f"  … {time.time()-t0:6.1f}s  elapsed  running={running[:1]} pending={pending}", flush=True)


def free_gpu():
    """ComfyUI のモデルをアンロードして VRAM を他のプロセスに返す。失敗は呼び出し元へ返す。"""
    print("\n=== /free (unload_models + free_memory) ===")
    api("/free", {"unload_models": True, "free_memory": True})
    print("  解放を要求しました")


def unload_engines():
    """ComfyUI に GPU/RAM を譲るため、共有している推論エンジンを unload する。

    次の推論要求で自動再ロードされる。対象が無い環境（未起動・unload API を持たない
    サービスが同じポートにいる）は警告して進める。status が返っているのに想定外な
    場合は、設定ミスで GPU を奪い合う恐れがあるので送信前に止まる。
    """
    if not ENGINES:
        print("\n=== 推論エンジン: 指定なしのため unload はしません ===")
        return
    for base, headers in ENGINES:
        print(f"\n=== {base}/v1/unload ===")
        try:
            result = api("/v1/unload", {}, base=base, headers=headers)
        except ComfyError as e:
            reason = getattr(e.__cause__, "reason", None)
            if getattr(reason, "errno", None) == errno.ECONNREFUSED:
                print(f"  起動していないため skip（{base} へ接続不能）")
                continue
            if str(e).startswith("接続不能"):
                raise ComfyError(f"推論エンジンを unload できないため ComfyUI には送信しません: {e}") from e
            print(f"  unload API を持つエンジンではない可能性が高いので skip（{str(e).splitlines()[0]}）")
            continue
        if not isinstance(result, dict) or "status" not in result:
            print("  unload API を持つエンジンではないため skip")
            continue
        status = result.get("status")
        if status not in ("unloaded", "not loaded"):
            raise ComfyError(f"{base} の unload が完了しませんでした (status={status!r})。ComfyUI には送信しません")
        print(f"  {status}（次の推論要求で再ロード）")


def collect(rec):
    items = [it for it in images_of(rec) if it.get("type") == "output"]
    print("  出力:", json.dumps(items, ensure_ascii=False)[:500])
    for it in items:
        path, size = download(it, OUT_DIR)
        print(f"  保存: {path} ({size/1048576:.2f} MB)")
    return items


def download(item, dest_dir):
    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                "type": item.get("type", "output")})
    url = f"{BASE}/view?{q}"
    with urllib.request.urlopen(url, timeout=120) as res:
        raw = res.read()
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, os.path.basename(item["filename"]))
    with open(path, "wb") as f:
        f.write(raw)
    return path, len(raw)


def flag_value(argv, flag):
    """--flag value / --flag=value の値。次のトークンが別のフラグなら値なし（None）。"""
    for i, tok in enumerate(argv):
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
        if tok == flag:
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                return argv[i + 1]
            return None
    return None


def flag_list(argv, flag):
    """同じフラグを複数回指定された値を順に返す。"""
    vals, i = [], 0
    while i < len(argv):
        tok = argv[i]
        if tok.startswith(flag + "="):
            vals.append(tok.split("=", 1)[1])
        elif tok == flag and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            vals.append(argv[i + 1])
            i += 1
        i += 1
    return vals


def has_flag(argv, flag):
    return any(tok == flag or tok.startswith(flag + "=") for tok in argv)


def state_dir():
    """このスキルの状態置き場。配布物（スキルフォルダ）には書かない。"""
    override = os.environ.get("COMFY_STATE_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    xdg = os.environ.get("XDG_STATE_HOME")
    root = xdg if xdg else os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(root, SKILL_ID)


def env_file_path(argv):
    p = flag_value(argv, "--env-file") or os.environ.get("COMFY_ENV_FILE")
    return os.path.abspath(os.path.expanduser(p)) if p else os.path.join(state_dir(), ENV_NAME)


def nodes_file_path():
    return os.path.join(os.path.dirname(ENV_PATH or env_file_path([])), NODES_NAME)


def write_json(path, payload):
    """途中まで書いたファイルが残らないよう、リネームで差し替える。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    for p, mode in ((os.path.dirname(path), 0o700), (path, 0o600)):
        try:
            os.chmod(p, mode)
        except OSError:
            pass


def read_env_file(argv):
    """env.json を読む。戻り値は (path, dict | None, 理由)。"""
    path = env_file_path(argv)
    if has_flag(argv, "--no-env"):
        return path, None, "--no-env で無効化"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return path, None, "env.json がまだ無い"
    except (OSError, ValueError) as e:
        return path, None, f"env.json が読めない（{type(e).__name__}: {e}）"
    if not isinstance(data, dict) or data.get("schema") != ENV_SCHEMA:
        got = data.get("schema") if isinstance(data, dict) else type(data).__name__
        return path, None, f"env.json の schema が合わない（{got} != {ENV_SCHEMA}）"
    return path, data, ""


def env_ttl(env):
    """保存された TTL。0 は「毎回検出」なので、falsy で既定値に落としてはいけない。"""
    try:
        return int(env.get("ttl_seconds", TTL_DEFAULT))
    except (TypeError, ValueError):
        return int(TTL_DEFAULT)


def graph_sha(wf_path):
    """同梱グラフの内容ハッシュ（スキル更新でモデル patch 名が変わったら検出し直させる）。"""
    try:
        with open(wf_path, "rb") as f:
            return sha_of(f.read().decode("utf-8", "replace"))
    except OSError:
        return ""


def detect_reason(argv, env, wf_path=None):
    """検出し直す理由（しないなら None）。未作成・TTL 切れ・グラフ変更なら自動で再検出する。"""
    if has_flag(argv, "--no-env"):
        return None
    if has_flag(argv, "--setup"):
        return "--setup"
    if has_flag(argv, "--refresh-env"):
        return "--refresh-env"
    if has_flag(argv, "--print-env"):
        return None      # 設定を見るだけのコマンドで、検出（= engine の unload）は走らせない
    if has_flag(argv, "--follow"):
        return None      # 追従だけの実行で、検出の再走（= engine への unload）はしない
    if has_flag(argv, "--no-auto-setup"):
        return None
    if env is None:
        return "env.json が無い/読めない"
    if wf_path and graph_sha(wf_path) != (env.get("fingerprint") or {}).get("workflow_sha256"):
        return "同梱グラフが変わった"
    if time.time() - float(env.get("generated_at") or 0) > env_ttl(env):
        return f"TTL {env_ttl(env)}s 切れ"
    return None


def sha_of(obj):
    if obj is None:
        return ""
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def load_nodes_cache():
    """nodes.json（object_info の写し）を復元する。fingerprint が変わった後は使わない。"""
    global NODE_DISK
    if not ENV or has_flag(sys.argv[1:], "--no-env"):
        return
    try:
        with open(nodes_file_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if data.get("fingerprint") != sha_of(ENV.get("fingerprint")):
        return
    if isinstance(data.get("nodes"), dict):
        NODE_DISK = data["nodes"]


def save_nodes_cache():
    global NODE_DIRTY
    if not ENV or not NODE_DIRTY or has_flag(sys.argv[1:], "--no-env"):
        return
    write_json(nodes_file_path(), {"schema": ENV_SCHEMA, "generated_at": int(time.time()),
                                   "fingerprint": sha_of(ENV.get("fingerprint")), "nodes": NODE_DISK})
    NODE_DIRTY = False


def role_of(class_type):
    """class_type が unet / clip / vae のどれを読み込むノードか（None なら_loader ではない）。"""
    key = class_type.lower().replace("_", "")
    for role, pats in LOADER_MATCHES.items():
        if any(pat in key for pat in pats):
            return role
    return None


def file_choices(spec):
    """object_info から、そのノードが受け取るファイル名の選択肢を順に並べる。"""
    out = []
    for decl in {**spec["input"].get("required", {}), **spec["input"].get("optional", {})}.values():
        if isinstance(decl, list) and decl and isinstance(decl[0], list):
            for opt in decl[0]:
                if isinstance(opt, str) and opt not in out:
                    out.append(opt)
    return out


def graph_classes(wf_path):
    """同梱グラフが実際に使う class_type（patch 後の名前も含む）を保証付き順で取る。"""
    with open(wf_path, encoding="utf-8") as f:
        graph = json.load(f)
    out, seen = [], set()
    for n in graph["nodes"]:
        if n["type"] in SKIP_TYPES or n["id"] in DROP_IDS:
            continue
        for ctype in (n["type"], PATCHES.get(n["id"], (None, None))[0]):
            if ctype and ctype not in seen:
                seen.add(ctype)
                out.append(ctype)
    return out


def graph_patch_models(wf_path):
    """同梱グラフが前提にしているモデルファイル名を role ごとに拾う（作者環境の値）。"""
    with open(wf_path, encoding="utf-8") as f:
        graph = json.load(f)
    types = {n["id"]: n["type"] for n in graph["nodes"]}
    out = {}
    for nid, (ctype, widgets) in PATCHES.items():
        if not widgets or not isinstance(widgets[0], str):
            continue
        role = role_of(ctype or types.get(nid, ""))
        if role:
            out[role] = (ctype or types.get(nid), widgets[0])
    return out


def closest_name(wanted, options):
    """この環境に無いモデル名の代わりに使えそうな名前（無ければ None）。"""
    if not wanted or not options:
        return None
    hits = difflib.get_close_matches(wanted, options, n=1, cutoff=0.6)
    if hits:
        return hits[0]
    if len(options) == 1:
        return options[0]
    return None


def detect_models(wf_path, env):
    """同梱グラフのモデル名がこの ComfyUI に実在するか確かめ、実在する名前を控える。"""
    wanted = graph_patch_models(wf_path)
    classes = env["classes"]
    out = {}
    for role in LOADER_MATCHES:
        cls, name = wanted.get(role, (None, None))
        entry = {"loader_class": cls, "name": name, "verified": name is None, "candidates": []}
        for ctype in classes:
            if role_of(ctype) != role:
                continue
            try:
                opts = file_choices(node_spec(ctype))
            except ComfyError as e:
                env["warnings"].append(f"{role}: {ctype} の選択肢が取れない（{str(e).splitlines()[0]}）")
                continue
            if not opts:
                continue
            entry["loader_class"] = ctype
            if not entry["candidates"]:
                entry["candidates"] = opts
            if name and name in opts:
                entry["name"] = name
                entry["verified"] = True
        if not entry["verified"] and entry["candidates"]:
            guess = closest_name(name, entry["candidates"])
            if guess:
                entry["name"] = guess
                entry["guessed"] = True
                env["warnings"].append(
                    f"{role}: {name!r} はこの ComfyUI に無い → {guess!r} を使う"
                    f"（違えば --{role} で上書き。候補は candidates を見る）")
            else:
                env["warnings"].append(
                    f"{role}: {name!r} に見合うファイルが無い。candidates から選んで --{role} を渡す")
        out[role] = entry
    return out


def detect_engines(argv):
    """GPU を譲る相手がこの機械にいるかを 1 回だけ確かめる。応答を見に行くため unload は走る。"""
    if has_flag(argv, "--no-unload-engine"):
        return []
    urls = flag_list(argv, "--unload-engine")
    if not urls:
        env_urls = os.environ.get("UNLOAD_ENGINE_URL") or os.environ.get("STRATA_URL")
        urls = [u.strip() for u in env_urls.split(",") if u.strip()] if env_urls else [ENGINE_DEFAULT]
    key = os.environ.get("UNLOAD_ENGINE_API_KEY") or os.environ.get("STRATA_API_KEY") or ""
    headers = {"Authorization": f"Bearer {key}"} if key else None
    out = []
    for url in urls:
        url = url.rstrip("/")
        rec = {"url": url, "unload_api": False, "state": ""}
        try:
            res = api("/v1/unload", {}, base=url, headers=headers)
            status = res.get("status") if isinstance(res, dict) else None
            rec["unload_api"] = status in ("unloaded", "not loaded")
            rec["state"] = str(status if status else "status が返らない")
        except ComfyError as e:
            reason = getattr(e.__cause__, "reason", None)
            first = str(e).splitlines()[0]
            if getattr(reason, "errno", None) == errno.ECONNREFUSED:
                rec["state"] = "起動していない"
            elif first.startswith("HTTP 404") or first.startswith("HTTP 405"):
                rec["state"] = "unload API を持たないサービス"
            else:
                rec["state"] = first
        out.append(rec)
    return out


def detect(argv, base, wf_path, prev=None):
    """この機械の ComfyUI を 1 回だけ調べて、env dict にまとめる（env.json に書く）。

    prev は以前の env.json。引数で明示していない値（出力先・keep_models）は引き継ぐ。
    """
    prev = prev or {}
    global BASE
    BASE = base
    keep = has_flag(argv, "--keep-models") or (
        not has_flag(argv, "--no-keep-models") and bool(prev.get("keep_models")))
    env = {"schema": ENV_SCHEMA,
           "generated_at": int(time.time()),
           "generated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "ttl_seconds": setting(argv, "env-ttl", "COMFY_ENV_TTL", TTL_DEFAULT, numeric=True),
           "comfy_url": base,
           "output_dir": setting(argv, "output-dir", "COMFY_OUTPUT_DIR",
                                prev.get("output_dir") or OUTPUT_DEFAULT, path=True),
           "keep_models": keep,
           "warnings": [],
           "comfy": {}, "devices": [], "classes": [], "models": {}, "engines": [],
           "fingerprint": {}}
    stats = api("/system_stats")
    sysinfo = stats.get("system") or {}
    env["comfy"] = {k: sysinfo.get(k) for k in
                    ("comfyui_version", "os", "python_version", "ram_total", "ram_free") if sysinfo.get(k) is not None}
    devices = stats.get("devices") or []
    env["devices"] = [{k: d.get(k) for k in ("name", "type", "index") if d.get(k) is not None}
                      for d in devices if isinstance(d, dict)]
    env["classes"] = graph_classes(wf_path)
    absent = [c for c in env["classes"] if not has_node(c)]
    if absent:
        env["warnings"].append(f"この ComfyUI に無いノードクラス: {', '.join(absent)}"
                               "（graph は patch を外して組み替える必要がある）")
    env["models"] = detect_models(wf_path, env)
    if "engines" in prev and not (has_flag(argv, "--setup") or has_flag(argv, "--refresh-env")):
        env["engines"] = prev["engines"]   # 自動再検出で engine を unload し直さない
        env["warnings"].append("engines は前の検出結果を維持（--setup で確認し直せる）")
    else:
        env["engines"] = detect_engines(argv)
    env["fingerprint"] = {
        "comfy": {k: sysinfo.get(k) for k in ("comfyui_version", "os", "python_version")},
        "devices": [d.get("name") for d in env["devices"]],
        "classes": sorted(env["classes"]),
        "workflow_sha256": graph_sha(wf_path),
    }
    return env


def print_env():
    """解決済みの設定と、env.json どの程度新しいかを表示する（送信も検出もしない）。"""
    print("=== env ===")
    print(f"  ファイル   : {ENV_PATH}")
    if ENV_NOTE:
        print(f"  状態       : {ENV_NOTE}")
    if ENV_REASON:
        print(f"  読み込み   : {ENV_REASON}")
    if ENV is None:
        print("  → env.json は使わず、引数・環境変数・既定値で動いています（--setup で検出できます）")
        return
    age = int(time.time() - float(ENV.get("generated_at") or 0))
    print(f"  検出       : {ENV.get('generated_at_iso', '?')}（{age}s 前 / TTL {env_ttl(ENV)}s）")
    print(f"  ComfyUI    : {BASE} / {ENV.get('comfy', {}).get('comfyui_version', '?')} / "
          f"{', '.join(d.get('name', '?') for d in ENV.get('devices') or []) or 'device 情報なし'}")
    print(f"  出力先     : {OUT_DIR}")
    for role, entry in (ENV.get("models") or {}).items():
        mark = "実在" if entry.get("verified") else ("推定" if entry.get("guessed") else "未検証")
        print(f"  {role:<5} : {entry.get('name')}（{entry.get('loader_class')} / {mark}"
              f" / 候補 {len(entry.get('candidates') or [])} 件）")
    for rec in ENV.get("engines") or []:
        print(f"  engine : {rec.get('url')} → {'unload API あり' if rec.get('unload_api') else rec.get('state')}")
    print(f"  keep_models: {bool(ENV.get('keep_models'))} / nodes.json キャッシュ: {len(NODE_DISK)} クラス")
    for w in ENV.get("warnings") or []:
        print(f"  WARNING: {w}")


def setting(argv, cli, env, default, *, json_key=None, path=False, numeric=False):
    """CLI 引数 > 環境変数 > env.json > 既定値 の順で 1 つの設定を取り出す。"""
    val = flag_value(argv, "--" + cli)
    if val is None and env:
        val = os.environ.get(env) or None
    if val is None and json_key and ENV:
        got = ENV.get(json_key)
        val = got if isinstance(got, str) and got else None
    if val is None:
        val = default
    if path and val:
        val = os.path.abspath(os.path.expanduser(val))
    if numeric:
        try:
            return int(val)
        except ValueError:
            raise ComfyError(f"数値でありません: {val!r}（--{cli} / {env}）")
    return val


def resolve_engines(argv):
    """GPU を譲りたい推論エンジン。明示指定 > 環境変数 > env.json（検出済み）> 既定 URL を 1 回試す。"""
    if has_flag(argv, "--no-unload-engine"):
        return []
    urls = flag_list(argv, "--unload-engine")
    if not urls:
        env_urls = os.environ.get("UNLOAD_ENGINE_URL") or os.environ.get("STRATA_URL")
        if env_urls:
            urls = [u.strip() for u in env_urls.split(",") if u.strip()]
    if not urls and ENV is not None:
        if "engines" in ENV:
            urls = [e["url"] for e in (ENV.get("engines") or []) if e.get("unload_api")]
        else:
            urls = [ENGINE_DEFAULT]      # 未検出なら従来どおり 1 回試して skip する
    if not urls and ENV is None:
        urls = [ENGINE_DEFAULT]
    key = os.environ.get("UNLOAD_ENGINE_API_KEY") or os.environ.get("STRATA_API_KEY") or ""
    headers = {"Authorization": f"Bearer {key}"} if key else None
    return [(u.rstrip("/"), headers) for u in urls]


def configure(argv):
    """引数・環境変数・env.json・既定値をまとめて解決し、モジュール設定に反映する。

    env.json が無い・TTL 切れなら、ここで 1 回だけこの機械を検出して書き直す。
    """
    global BASE, WF_IN, API_OUT, OUT_DIR, WAIT_TIMEOUT, SEED, MODELS, ENGINES
    global ENV, ENV_PATH, ENV_NOTE, ENV_REASON
    ENV_PATH, ENV, ENV_REASON = read_env_file(argv)
    WF_IN = setting(argv, "workflow", "COMFY_WORKFLOW", WORKFLOW_DEFAULT, path=True)
    reason = detect_reason(argv, ENV, WF_IN)
    if reason:
        base = setting(argv, "comfy-url", "COMFY_URL", (ENV or {}).get("comfy_url") or COMFY_DEFAULT).rstrip("/")
        ENV = detect(argv, base, WF_IN, ENV)
        write_json(ENV_PATH, ENV)
        ENV_NOTE = f"{reason} のため検出して更新" + (f"（読み込み: {ENV_REASON}）" if ENV_REASON else "")
        ENV_REASON = ""
        save_nodes_cache()
    BASE = setting(argv, "comfy-url", "COMFY_URL", COMFY_DEFAULT, json_key="comfy_url").rstrip("/")
    OUT_DIR = setting(argv, "output-dir", "COMFY_OUTPUT_DIR", OUTPUT_DEFAULT, json_key="output_dir", path=True)
    WAIT_TIMEOUT = setting(argv, "timeout", "WAIT_TIMEOUT", WAIT_DEFAULT, numeric=True)
    SEED = setting(argv, "seed", "SEED", SEED_DEFAULT, numeric=True)
    MODELS = {}
    for role in LOADER_MATCHES:
        entry = (ENV.get("models") or {}).get(role) or {} if ENV else {}
        MODELS[role] = setting(argv, role, f"MOSS_{role.upper()}", entry.get("name"))
    ENGINES = resolve_engines(argv)
    load_nodes_cache()
    keep_models = has_flag(argv, "--keep-models") or (
        not has_flag(argv, "--no-keep-models") and bool((ENV or {}).get("keep_models")))
    debug_path = flag_value(argv, "--debug")
    API_OUT = os.path.abspath(os.path.expanduser(debug_path)) if debug_path \
        else os.path.join(os.getcwd(), DEBUG_NAME)
    return {"dry": has_flag(argv, "--dry-run"), "debug": has_flag(argv, "--debug"),
            "setup": has_flag(argv, "--setup"), "print_env": has_flag(argv, "--print-env"),
            "keep_models": keep_models}


LATENT_TYPES = {"EmptyLatentImage", "EmptySD3LatentImage", "EmptyFlux2LatentImage",
                "EmptyMMDiTLatentImage", "EmptyWanLatentImage", "EmptyLTXVLatentImage"}
KSAMPLER_TYPES = {"KSampler", "KSamplerAdvanced", "KSamplerAdvancedGuider"}


def apply_overrides(prompt, notes):
    """基準ワークフローに手を付けず、CLI で渡したものだけを適用する。"""
    argv = sys.argv[1:]
    pf = flag_value(argv, "--prompt-file")
    if pf:
        text = open(os.path.expanduser(pf), encoding="utf-8").read().strip()
        hits = []
        for nid, p in prompt.items():
            if p["class_type"] == "CLIPTextEncode" and "text" in p["inputs"]:
                p["inputs"]["text"] = text
                hits.append(nid)
        notes.append(f"positive を {pf} に差し替え（ノード {hits}）")

    nf = flag_value(argv, "--negative-file")
    if nf:
        text = open(os.path.expanduser(nf), encoding="utf-8").read().strip()
        clip_src, zero_ids = None, []
        for nid, p in prompt.items():
            if p["class_type"] == "CLIPTextEncode" and isinstance(p["inputs"].get("clip"), list):
                clip_src = p["inputs"]["clip"]
            if p["class_type"] == "ConditioningZeroOut":
                zero_ids.append(nid)
        if not clip_src:
            raise ComfyError("negative 用の CLIP 接続を取れませんでした")
        new_id = "90"
        while new_id in prompt:
            new_id = str(int(new_id) + 1)
        prompt[new_id] = {"class_type": "CLIPTextEncode",
                          "inputs": {"clip": clip_src, "text": text},
                          "_meta": {"title": "Prompt (negative)"}}
        for nid, p in prompt.items():
            for key, val in p["inputs"].items():
                if isinstance(val, list) and str(val[0]) in zero_ids:
                    p["inputs"][key] = [new_id, val[1]]
                    notes.append(f"[{nid}] {key} を空の負条件 → 負プロンプトノード {new_id} に付け替え")
        for zid in zero_ids:
            prompt.pop(zid, None)
            notes.append(f"[{zid}] ConditioningZeroOut を削除")

    size = flag_value(argv, "--size")
    if size:
        w, h = (int(v) for v in re.split(r"[x*]", size.lower().strip()))
        if w % 16 or h % 16:
            raise ComfyError(f"解度は 16 の倍数にしてください: {w}x{h}")
        for nid, p in prompt.items():
            if p["class_type"] in LATENT_TYPES:
                p["inputs"]["width"], p["inputs"]["height"] = w, h
                notes.append(f"[{nid}] 解度を {w}x{h} に変更")

    sampler, scheduler, steps, cfg = (flag_value(argv, x) for x in
                                      ("--sampler", "--scheduler", "--steps", "--cfg"))
    for nid, p in prompt.items():
        if p["class_type"] in KSAMPLER_TYPES:
            for key, val in (("sampler_name", sampler), ("scheduler", scheduler),
                             ("steps", int(steps) if steps else None),
                             ("cfg", float(cfg) if cfg else None)):
                if val is not None and key in p["inputs"]:
                    p["inputs"][key] = val
                    notes.append(f"[{nid}] {key} = {val}")

    prefix = flag_value(argv, "--prefix")
    if prefix:
        for nid, p in prompt.items():
            if "filename_prefix" in p["inputs"]:
                p["inputs"]["filename_prefix"] = prefix
                notes.append(f"[{nid}] filename_prefix = {prefix}")
    return notes


def fail(msg, code=1):
    """stdout を書き終えてからエラーを出す（混在したまま tail されると順番が入れ替わる）。"""
    sys.stdout.flush()
    print(f"\nNG: {msg}", file=sys.stderr)
    sys.exit(code)


def error_hint(msg):
    """接続できない・モデル名が無い、のときは検出コマンドを添える（毎回調べに戻らなくて済むように）。"""
    if msg.startswith("接続不能"):
        return ("\n  ヒント: ComfyUI が起動していないか URL が違います。"
                "--setup --comfy-url http://127.0.0.1:8188 で検出し直すと env.json に保存されます")
    if "差し替え先が見つかりませんでした" in msg or "ノード定義がありません" in msg:
        return "\n  ヒント: --setup で環境を検出し直し、env.json の models[*].candidates から選んでください"
    return ""


def run(argv):
    cfg = configure(argv)
    keep_models = cfg["keep_models"]
    if cfg["print_env"]:
        print_env()
        return
    if cfg["setup"]:
        print(f"\n=== --setup 完了: {ENV_PATH} ===")
        print_env()
        return
    if keep_models:
        print("WARNING: --keep-models は ComfyUI の VRAM を保持するため、同じ GPU を使う推論エンジンが再ロードできない場合があります。")
    env_src = ENV_NOTE or ENV_REASON or "読み込み済み"
    models_line = " / ".join(f"{r}={MODELS[r] or 'graph 内蔵'}" for r in LOADER_MATCHES)
    print(f"=== 設定 ===\n  ComfyUI : {BASE}\n  ワークフロー: {WF_IN}\n  出力先  : {OUT_DIR}"
          f"\n  seed      : {SEED} / 待ち上限 {WAIT_TIMEOUT}s"
          f"\n  models    : {models_line}"
          f"\n  推論エンジン: {', '.join(u for u, _ in ENGINES) or 'なし（解放しません）'}"
          f"\n  env.json  : {ENV_PATH}（{env_src}）")
    pid = flag_value(argv, "--follow")
    if has_flag(argv, "--follow") and not pid:
        raise ComfyError("--follow には prompt_id を付けてください（--follow <PROMPT_ID>）")
    if pid:
        unload_engines()
        t0 = time.time()
        print(f"=== 送信済みジョブを追従（{pid}）===")
        try:
            collect(wait_done(pid, t0))
        finally:
            if not keep_models:
                free_gpu()
        return

    dry, debug = cfg["dry"], cfg["debug"]
    graph = json.load(open(WF_IN))
    prompt, notes = convert(graph)
    notes = apply_overrides(prompt, notes)
    notes = apply_model_overrides(prompt, notes)
    notes.extend(apply_seed(prompt, SEED))

    print("=== 環境差し替え ===")
    for m in notes:
        print("  ", m)
    print(f"\n=== 変換結果（{len(prompt)} ノード）===")
    for nid, p in sorted(prompt.items(), key=lambda kv: int(kv[0])):
        vals = {k: v for k, v in p["inputs"].items()}
        print(f"  [{nid}] {p['class_type']}")
        for k, v in vals.items():
            s = json.dumps(v, ensure_ascii=False)
            print(f"      {k:<28} {s[:90]}{'…' if len(s) > 90 else ''}")

    if debug:
        json.dump(prompt, open(API_OUT, "w"), ensure_ascii=False, indent=2)
        print(f"\n→ {API_OUT} に保存")

    if dry:
        print("（--dry-run なので推論エンジンは unload せず、ComfyUI に生成を送信しません）")
        return

    # ここでユーザーへの応答はすでに終わっている前提。同じ GPU を使うエンジンがあれば
    # 解放してから ComfyUI に送り、生成中に両モデルが VRAM/RAM を取り合わない。
    unload_engines()
    prompt_attempted = False
    try:
        print(f"\n=== /prompt に送信（seed={SEED}）===")
        t0 = time.time()
        prompt_attempted = True
        res = api("/prompt", {"prompt": prompt})
        pid = res.get("prompt_id")
        if not pid:
            raise ComfyError("prompt_id が返らなかった: " + json.dumps(res, ensure_ascii=False)[:800])
        print("  prompt_id:", pid)

        rec = wait_done(pid, t0)
        print(f"\n=== 完了（{time.time()-t0:.1f}s）===")
        collect(rec)
    finally:
        # 送信後の失敗やタイムアウトでも ComfyUI のモデルを解放してから元に戻す。
        # --keep-models は明示的な例外で、同じ GPU を使う推論エンジンが VRAM 不足になる可能性がある。
        if prompt_attempted and not keep_models:
            free_gpu()


def main():
    argv = sys.argv[1:]
    try:
        run(argv)
    finally:
        # 今回取った object_info を次回以降の起動で再利用する（HTTP を減らす）
        save_nodes_cache()


if __name__ == "__main__":
    try:
        main()
    except (ComfyError, TimeoutError) as e:
        fail(str(e) + error_hint(str(e)))
    except FileNotFoundError as e:
        fail(f"ファイルがありません: {e.filename}"
             "（--workflow / --prompt-file / --negative-file のパスを確認してください）")
    except KeyboardInterrupt:
        fail("中断しました", 130)
