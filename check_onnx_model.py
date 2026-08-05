from funasr_onnx import SenseVoiceSmall
import os

# funasr-onnx 的 SenseVoiceSmall 接受 modelscope 模型名，会自动下载并导出 ONNX
# 但如果 cache 里已有 model.onnx 就可以直接加载
model_dir = os.path.expanduser("~/.cache/modelscope/hub/models/iic/SenseVoiceSmall")

# 先检查是否有 model.onnx
onnx_path = os.path.join(model_dir, "model.onnx")
quant_path = os.path.join(model_dir, "model_quant.onnx")
print(f"model.onnx exists: {os.path.exists(onnx_path)}")
print(f"model_quant.onnx exists: {os.path.exists(quant_path)}")
print(f"model_dir contents: {os.listdir(model_dir)}")
