#!/usr/bin/env python3
"""ComfyUI の UI(graph) form ワークフローを API form に変換して実行し、出力画像を取り出す。

Python 標準ライブラリだけで動く。接続先・モデル名・出力先などの環境差は、
すべて「CLI 引数 > 環境変数 > このファイルの既定値」の順で解決する。
既定値は作者のローカル環境向けなので、他の環境では引数か環境変数で上書きするか、
--workflow でその環境のワークフローを渡す。

使い方:
    python3 comfy_run.py                 # 変換して送信し、完了を待って画像を取り出す
    python3 comfy_run.py --dry-run       # 変換結果を表示するだけ（送信しない）
    python3 comfy_run.py --follow PID    # 送信済みジョブの追従だけ行う
    python3 comfy_run.py --debug [PATH]  # 変換後の API form JSON を保存して中身を確かめる（既定では書かない）
    python3 comfy_run.py --keep-models   # 生成後に ComfyUI のモデルを降ろさない

接続先・ファイル・環境差:
    --comfy-url URL     / COMFY_URL          ComfyUI の API base（既定 http://127.0.0.1:8188）
    --workflow PATH     / COMFY_WORKFLOW     基準にする UI form ワークフロー（既定は同梱の moss_image_sft.workflow.json）
    --output-dir PATH   / COMFY_OUTPUT_DIR   生成画像の保存先（既定 ~/Pictures/ComfyUI）
    --unet NAME         / MOSS_UNET          ┐ この ComfyUI に実在するモデルファイル名。
    --clip NAME         / MOSS_CLIP          ├ 同梱グラフは作者のモデル名に patch されているので、
    --vae NAME          / MOSS_VAE           ┘ 他の環境では必ず自環境の名前を渡す
    --seed N            / SEED               再現用 seed
    --timeout N         / WAIT_TIMEOUT       完了待ちの上限秒数
    --unload-engine URL                     GPU を共有している推論エンジンの base URL（複数指定可）
    --no-unload-engine                      エンジン解放を一切しない
    UNLOAD_ENGINE_URL   旧 STRATA_URL        カンマ区切りで複数
    UNLOAD_ENGINE_API_KEY / 旧 STRATA_API_KEY  Bearer トークン

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
import errno
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


def node_spec(class_type, cache={}):
    if class_type not in cache:
        d = api(f"/object_info/{class_type}")
        if class_type not in d:
            raise ComfyError(f"ノード定義がありません: {class_type}")
        cache[class_type] = d[class_type]
    return cache[class_type]


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
                             f"\n  その環境では別ノードの可能性があるので、自環境のワークフローを --workflow で渡してください")
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


def setting(argv, cli, env, default, *, path=False, numeric=False):
    """CLI 引数 > 環境変数 > 既定値 の順で 1 つの設定を取り出す。"""
    val = flag_value(argv, "--" + cli)
    if val is None and env:
        val = os.environ.get(env) or None
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
    """GPU を譲りたい推論エンジンの URL 群。明示的に無効化されると空になる。"""
    if has_flag(argv, "--no-unload-engine"):
        return []
    urls = flag_list(argv, "--unload-engine")
    if not urls:
        env = os.environ.get("UNLOAD_ENGINE_URL") or os.environ.get("STRATA_URL") or ENGINE_DEFAULT
        urls = [u.strip() for u in env.split(",") if u.strip()]
    key = os.environ.get("UNLOAD_ENGINE_API_KEY") or os.environ.get("STRATA_API_KEY") or ""
    headers = {"Authorization": f"Bearer {key}"} if key else None
    return [(u.rstrip("/"), headers) for u in urls]


def configure(argv):
    """引数・環境変数・既定値をまとめて解決し、モジュール設定に反映する。"""
    global BASE, WF_IN, API_OUT, OUT_DIR, WAIT_TIMEOUT, SEED, MODELS, ENGINES
    BASE = setting(argv, "comfy-url", "COMFY_URL", COMFY_DEFAULT).rstrip("/")
    WF_IN = setting(argv, "workflow", "COMFY_WORKFLOW", WORKFLOW_DEFAULT, path=True)
    OUT_DIR = setting(argv, "output-dir", "COMFY_OUTPUT_DIR", OUTPUT_DEFAULT, path=True)
    WAIT_TIMEOUT = setting(argv, "timeout", "WAIT_TIMEOUT", WAIT_DEFAULT, numeric=True)
    SEED = setting(argv, "seed", "SEED", SEED_DEFAULT, numeric=True)
    MODELS = {role: setting(argv, role, f"MOSS_{role.upper()}", None) for role in LOADER_MATCHES}
    ENGINES = resolve_engines(argv)
    debug_path = flag_value(argv, "--debug")
    API_OUT = os.path.abspath(os.path.expanduser(debug_path)) if debug_path \
        else os.path.join(os.getcwd(), DEBUG_NAME)
    return {"dry": has_flag(argv, "--dry-run"), "debug": has_flag(argv, "--debug"),
            "keep_models": has_flag(argv, "--keep-models")}


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


def main():
    argv = sys.argv[1:]
    cfg = configure(argv)
    keep_models = cfg["keep_models"]
    if keep_models:
        print("WARNING: --keep-models は ComfyUI の VRAM を保持するため、同じ GPU を使う推論エンジンが再ロードできない場合があります。")
    print(f"=== 設定 ===\n  ComfyUI : {BASE}\n  ワークフロー: {WF_IN}\n  出力先  : {OUT_DIR}"
          f"\n  seed      : {SEED} / 待ち上限 {WAIT_TIMEOUT}s"
          f"\n  推論エンジン: {', '.join(u for u, _ in ENGINES) or 'なし（解放しません）'}")
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


if __name__ == "__main__":
    try:
        main()
    except (ComfyError, TimeoutError) as e:
        fail(str(e))
    except FileNotFoundError as e:
        fail(f"ファイルがありません: {e.filename}"
             "（--workflow / --prompt-file / --negative-file のパスを確認してください）")
    except KeyboardInterrupt:
        fail("中断しました", 130)
