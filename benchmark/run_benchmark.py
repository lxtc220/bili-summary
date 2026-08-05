"""
A vs B2 公平对比(同引擎 funasr + 同设备 CUDA + 不同模型):
  A:  Paraformer-zh   (本地路径)
  B2: SenseVoiceSmall (英文路径,绕过 sentencepiece 中文路径 bug)
公平:都用 fsmn-vad 切段、>5min 硬切兜底、同一台 GPU、预热1次。
"""
import os, sys, time, gc, json
sys.stdout.reconfigure(line_buffering=True)

os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'
import torch
from funasr import AutoModel
from pydub import AudioSegment

AUDIO = "benchmark/test.wav"
AUDIO_LEN_S = 1587.584

VAD_PATH = "model_cache/models/iic/fsmn-vad"
PARAFORMER_PATH = "model_cache/models/iic/paraformer-zh"
SENSEVOICE_PATH = "C:/temp/sv_model"  # 英文路径,绕过 sentencepiece 中文路径段错误
MAX_SEG_MS = 300000

device = "cuda"


def split_audio_segment_by_range(audio, start_ms, end_ms, seg_ms=MAX_SEG_MS):
    segs = []
    cur = start_ms
    while cur < end_ms:
        e = min(cur + seg_ms, end_ms)
        segs.append((cur, e, audio[cur:e]))
        cur = e
    return segs


def run_vad(vad_model, audio_path):
    res = vad_model.generate(input=audio_path, cache={}, disable_pbar=True)
    segs = []
    if isinstance(res, list) and len(res) > 0:
        for seg in res[0].get("value", []):
            try:
                s, e = int(float(seg[0])), int(float(seg[1]))
                if e > s:
                    segs.append((s, e))
            except Exception:
                continue
    return segs


def transcribe(model_name, asr_path, segments, audio, n_total, warmup=False):
    """加载模型 + 逐段推理。"""
    torch.cuda.empty_cache(); gc.collect()

    t0 = time.time()
    asr = AutoModel(model=asr_path, trust_remote_code=False, device=device, disable_update=True)
    load_time = time.time() - t0
    if not warmup:
        print(f"  [{model_name}] 加载: {load_time:.2f}s", flush=True)

    os.makedirs("intermediate_files", exist_ok=True)
    all_texts = []
    t_infer_start = time.time()
    for i, (s_ms, e_ms, seg_audio) in enumerate(segments):
        temp = f"intermediate_files/bench_{model_name}_{i}.wav"
        seg_audio.export(temp, format="wav")
        try:
            if model_name == "sensevoice":
                res = asr.generate(input=temp, language="auto", use_itn=True)
            else:
                res = asr.generate(input=temp, batch_size_s=30)
            if isinstance(res, list) and len(res) > 0:
                all_texts.append(res[0]["text"].replace(" ", ""))
        except Exception as ex:
            if not warmup:
                print(f"  [{model_name}] 第{i+1}段失败: {ex}", flush=True)
        finally:
            if os.path.exists(temp):
                os.remove(temp)
        if device == "cuda":
            torch.cuda.empty_cache()
        if not warmup and (i+1) % 5 == 0:
            print(f"  [{model_name}] 进度 {i+1}/{n_total}", flush=True)
    infer_time = time.time() - t_infer_start

    del asr
    torch.cuda.empty_cache(); gc.collect()
    return load_time, infer_time, "".join(all_texts)


def main():
    print(f"设备: {device} ({torch.cuda.get_device_name(0)})")
    print(f"音频: {AUDIO}  时长 {AUDIO_LEN_S:.1f}s ({AUDIO_LEN_S/60:.1f}min)")

    # VAD 切段(共用)
    print("\n=== 加载 VAD 切段 ===", flush=True)
    torch.cuda.empty_cache(); gc.collect()
    vad = AutoModel(model=VAD_PATH, trust_remote_code=False, device=device, disable_update=True)
    vad_segs = run_vad(vad, AUDIO)
    audio = AudioSegment.from_file(AUDIO)
    segments = []
    for (s, e) in vad_segs:
        if e - s > MAX_SEG_MS:
            segments.extend(split_audio_segment_by_range(audio, s, e, MAX_SEG_MS))
        else:
            segments.append((s, e, audio[s:e]))
    del vad
    torch.cuda.empty_cache(); gc.collect()
    print(f"VAD段: {len(vad_segs)}, 最终段: {len(segments)}", flush=True)

    results = {}
    for name, path in [("paraformer", PARAFORMER_PATH), ("sensevoice", SENSEVOICE_PATH)]:
        print(f"\n=== 预热 {name} ===", flush=True)
        try:
            transcribe(name, path, segments[:1], audio, len(segments), warmup=True)
        except Exception as e:
            print(f"  预热失败: {e}")
        torch.cuda.empty_cache(); gc.collect()

        print(f"=== 正式测试 {name} ===", flush=True)
        t_total = time.time()
        load_t, infer_t, text = transcribe(name, path, segments, audio, len(segments))
        total_t = time.time() - t_total
        results[name] = {
            "load_s": round(load_t, 2),
            "infer_s": round(infer_t, 2),
            "total_s": round(total_t, 2),
            "text_len": len(text),
            "rtf": round(infer_t / AUDIO_LEN_S, 4),
        }
        print(f"  加载:{load_t:.2f}s 推理:{infer_t:.2f}s 总计:{total_t:.2f}s RTF={infer_t/AUDIO_LEN_S:.3f} 文本{len(text)}字", flush=True)
        torch.cuda.empty_cache(); gc.collect()

    # 对比表
    print("\n" + "=" * 64, flush=True)
    print("对比结果 (A: Paraformer vs B2: SenseVoice, 同 GPU)", flush=True)
    print("=" * 64, flush=True)
    print(f"{'指标':<12}{'Paraformer':>16}{'SenseVoice':>16}{'差异':>16}", flush=True)
    for k, label in [("load_s","加载(s)"), ("infer_s","推理(s)"), ("total_s","总计(s)"), ("rtf","RTF")]:
        p, s = results["paraformer"][k], results["sensevoice"][k]
        diff = f"{s-p:+.2f}" if k != "rtf" else f"{s-p:+.3f}"
        print(f"{label:<12}{p:>16}{s:>16}{diff:>16}", flush=True)
    pf, sf = results["paraformer"]["infer_s"], results["sensevoice"]["infer_s"]
    if sf > 0:
        print(f"\nSenseVoice 推理速度是 Paraformer 的 {pf/sf:.2f}x", flush=True)

    with open("benchmark/result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n结果已保存到 benchmark/result.json", flush=True)


if __name__ == "__main__":
    main()
