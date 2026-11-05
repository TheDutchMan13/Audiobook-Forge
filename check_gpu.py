import torch

print("torch:", torch.__version__)
print("GPU available:", torch.cuda.is_available())
if torch.cuda.is_available():
    for i in range(torch.cuda.device_count()):
        print(f"device [{i}]:", torch.cuda.get_device_name(i))
    x = torch.randn(2048, 2048, device="cuda")
    y = (x @ x).sum().item()
    print("matmul on GPU OK:", y == y)
