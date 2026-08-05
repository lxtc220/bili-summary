"""单独验证 SenseVoiceSmall 是否真的在 GPU 上跑。"""
import sys, torch
from funasr import AutoModel

sys.stdout.reconfigure(line_buffering=True)
print("=== 加载 SenseVoiceSmall (device=cuda) ===")
m = AutoModel(
    model="model_cache/models/iic/SenseVoiceSmall",
    trust_remote_code=True,
    device="cuda",
    disable_update=True,
)

# 1. 看参数在哪个设备
for name, p in m.model.named_parameters():
    print(f"参数设备: {p.device}  (样例: {name})")
    break

# 2. 看 kwargs 里 device
print(f"kwargs device: {m.kwargs.get('device')}")

# 3. 推理前后显存对比
torch.cuda.empty_cache()
print(f"推理前 allocated: {torch.cuda.memory_allocated()/1024**2:.1f} MB")

from pydub import AudioSegment
AudioSegment.from_wav("benchmark/test.wav")[:30000].export("bench_30s.wav", format="wav")

print("=== 推理 30 秒样本 ===")
res = m.generate(input="bench_30s.wav", language="auto", use_itn=True)
print(f"推理后 allocated: {torch.cuda.memory_allocated()/1024**2:.1f} MB")
print(f"文本前60字: {res[0]['text'][:60]}")
