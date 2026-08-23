---
name: pytorch-training
description: >-
  Use when writing or debugging a PyTorch training loop. Covers the correct loop
  structure, Dataset and DataLoader, device handling, reproducibility, and how to
  diagnose a loss that will not go down.
requires: [pytorch]
---

# PyTorch training

## The loop, correctly

```python
model.train()
for epoch in range(epochs):
    for batch in train_loader:
        inputs, targets = (t.to(device, non_blocking=True) for t in batch)

        optimizer.zero_grad(set_to_none=True)
        outputs = model(inputs)
        loss = criterion(outputs, targets)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        scheduler.step()          # per-batch schedulers only

    validate(model, val_loader, device)
```

The mistakes this avoids, each of which trains a model that silently underperforms:

- **Missing `zero_grad`** — gradients accumulate across batches. Loss looks
  erratic and the model barely learns.
- **`model.train()` / `model.eval()` not toggled** — dropout and batch-norm behave
  differently. Validating in train mode gives noisy, optimistic numbers; training
  in eval mode disables regularisation entirely.
- **`loss.item()` in the graph** — accumulating `total += loss` instead of
  `loss.item()` keeps the whole computation graph alive and leaks memory until OOM.
- **Scheduler stepped in the wrong place** — per-epoch schedulers step outside the
  batch loop; `ReduceLROnPlateau` steps with the validation metric, after validating.

## Validation

```python
@torch.no_grad()
def validate(model, loader, device):
    model.eval()
    total, count = 0.0, 0
    for inputs, targets in loader:
        inputs, targets = inputs.to(device), targets.to(device)
        loss = criterion(model(inputs), targets)
        total += loss.item() * inputs.size(0)
        count += inputs.size(0)
    model.train()
    return total / count
```

`@torch.no_grad()` roughly halves memory and speeds validation up. Weight by batch
size — averaging per-batch means is wrong when the last batch is smaller.

## Dataset and DataLoader

```python
class RecordDataset(Dataset):
    def __init__(self, paths, transform=None):
        self.paths = paths          # cheap: no loading here
        self.transform = transform

    def __len__(self):
        return len(self.paths)

    def __getitem__(self, index):   # loading happens here, per worker
        item = load(self.paths[index])
        return self.transform(item) if self.transform else item
```

Do the loading in `__getitem__`, not `__init__` — otherwise the whole dataset sits
in memory and is copied to every worker.

```python
loader = DataLoader(
    dataset,
    batch_size=64,
    shuffle=True,          # train only; never for validation
    num_workers=4,
    pin_memory=True,       # with CUDA
    drop_last=True,        # train only, keeps batch-norm stable
    persistent_workers=True,
)
```

If GPU utilisation is low and spiky, the data loader is the bottleneck, not the
model. Raise `num_workers`, and check you are not doing heavy work per item that
could be precomputed.

## Devices

Resolve the device once and pass it down:

```python
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = model.to(device)
```

`Tensor.to()` returns a new tensor and does **not** modify in place; `Module.to()`
does modify in place. `x.to(device)` without assignment is a silent no-op and a
very common bug.

## Reproducibility

```python
def seed_everything(seed: int = 0) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
```

`cudnn.benchmark = True` is faster but non-deterministic; pick one and say which.
Also seed the DataLoader workers (`worker_init_fn`) — they get their own RNG state.

## Checkpointing

```python
torch.save({
    "epoch": epoch,
    "model": model.state_dict(),
    "optimizer": optimizer.state_dict(),
    "scheduler": scheduler.state_dict(),
    "best_metric": best,
}, path)
```

Save the optimizer and scheduler state, not just the weights. Resuming with a
fresh optimizer loses momentum and adaptive moments, and the loss visibly jumps.

Save `state_dict()`, never the pickled model object — the latter breaks when the
class moves.

## When the loss will not go down

Work through it in this order. Do not change three things at once.

1. **Overfit a single batch.** Take 8 examples and train until the loss is
   ~0. If it cannot, there is a bug — not a tuning problem. This is the highest
   value five minutes in deep learning.
2. **Check the learning rate.** Too high: loss is NaN or oscillates. Too low: it
   decreases imperceptibly. Sweep by orders of magnitude, not by 10%.
3. **Check the labels reach the loss.** Print shapes and a batch of targets.
   Mismatched shapes broadcast silently and produce a meaningless loss.
4. **Check the loss function.** `CrossEntropyLoss` expects **raw logits** and
   class indices — applying softmax first is a common and quiet error.
   `BCELoss` expects probabilities; `BCEWithLogitsLoss` expects logits and is more
   numerically stable.
5. **Check normalisation.** Unnormalised inputs make optimisation hard.
6. **Check gradient flow** — `p.grad.norm()` per layer. All zeros means something
   is detached; huge values mean you need clipping.

## Loss is NaN

Almost always one of: learning rate too high; `log(0)` or division by zero in a
custom loss; missing gradient clipping with RNNs; or fp16 overflow (use
`torch.amp.GradScaler`). Find the first NaN batch with
`torch.autograd.set_detect_anomaly(True)` — slow, so only while debugging.
