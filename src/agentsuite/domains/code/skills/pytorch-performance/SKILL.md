---
name: pytorch-performance
description: >-
  Use when PyTorch training is slow, or hits CUDA out-of-memory. Covers finding
  the real bottleneck, mixed precision, memory reduction, DataLoader tuning and
  compilation - in the order that actually pays off.
requires: [pytorch]
---

# PyTorch performance and memory

## Find the bottleneck before optimising

Three very different problems look the same from the outside.

```bash
nvidia-smi dmon -s u        # GPU utilisation over time
```

| Symptom | Bottleneck | Fix |
|---|---|---|
| GPU util low and spiky | data loading | more workers, cheaper transforms, precompute |
| GPU util pegged at ~100% | compute | AMP, larger batch, `torch.compile` |
| GPU util low and flat | CPU-side sync, tiny kernels | remove `.item()`/`.cpu()` from the loop |
| Fine, then OOM after N steps | a leak | accumulating tensors with graphs attached |

For anything more precise:

```python
with torch.profiler.profile(
    activities=[torch.profiler.ProfilerActivity.CPU, torch.profiler.ProfilerActivity.CUDA],
    schedule=torch.profiler.schedule(wait=1, warmup=1, active=3),
) as prof:
    for step, batch in enumerate(loader):
        train_step(batch)
        prof.step()
        if step > 6:
            break
print(prof.key_averages().table(sort_by="cuda_time_total", row_limit=15))
```

**CUDA is asynchronous.** Naive `time.time()` around a forward pass measures
launch time, not execution. Call `torch.cuda.synchronize()` before timing, or use
the profiler.

## Mixed precision — the cheapest large win

Typically 1.5–3× faster and roughly half the activation memory on modern GPUs.

```python
scaler = torch.amp.GradScaler("cuda")

for batch in loader:
    optimizer.zero_grad(set_to_none=True)
    with torch.amp.autocast("cuda", dtype=torch.bfloat16):
        loss = criterion(model(inputs), targets)
    scaler.scale(loss).backward()
    scaler.unscale_(optimizer)                      # before clipping
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
    scaler.step(optimizer)
    scaler.update()
```

`bfloat16` on Ampere and later: same exponent range as fp32, so overflow is not a
concern and the `GradScaler` is largely a formality. `float16` on older hardware
needs the scaler. Note `unscale_` before clipping — clipping scaled gradients
clips the wrong magnitude.

## Out of memory

In order of how much they cost you:

1. **Reduce batch size**, and recover the effective batch with accumulation:
   ```python
   for i, batch in enumerate(loader):
       with torch.amp.autocast("cuda", dtype=torch.bfloat16):
           loss = criterion(model(batch.x), batch.y) / accum_steps
       scaler.scale(loss).backward()
       if (i + 1) % accum_steps == 0:
           scaler.step(optimizer); scaler.update()
           optimizer.zero_grad(set_to_none=True)
   ```
2. **Mixed precision** (above).
3. **`set_to_none=True`** on `zero_grad` — frees the gradient buffers rather than
   zeroing them.
4. **Gradient checkpointing** — recompute activations in the backward pass.
   Roughly 30% slower, and a large memory saving for deep models:
   ```python
   from torch.utils.checkpoint import checkpoint_sequential
   ```
5. **`torch.no_grad()` for inference and validation.** Frequently forgotten and
   frequently the whole problem.

**Diagnosing a leak.** Memory that grows every step is almost always a tensor
retained with its graph:

```python
total_loss += loss          # WRONG -- keeps the whole graph, every step
total_loss += loss.item()   # right
```

Also check for tensors appended to a list without `.detach()`, and for a
`hidden` state carried between RNN steps without `.detach()`.

`torch.cuda.empty_cache()` does not fix a leak — it only returns cached blocks to
the driver. Reaching for it is a sign you have not found the real cause.

## DataLoader

- `num_workers`: start at 4, raise while GPU utilisation improves. Zero means
  loading happens in the training process and blocks it.
- `pin_memory=True` with `non_blocking=True` on `.to()` overlaps transfer with
  compute.
- `persistent_workers=True` avoids re-spawning workers each epoch — noticeable
  with short epochs.
- Precompute expensive transforms once to disk rather than every epoch.
- Prefer a memory-mapped or columnar format over thousands of small files.

## Compilation

```python
model = torch.compile(model)      # PyTorch 2.x
```

Often 10–30% for free. Caveats worth knowing: the first step is slow (compilation);
varying input shapes trigger recompilation, so pad to fixed shapes or set
`dynamic=True`; and it complicates tracebacks, so debug uncompiled.

## Things that quietly cost a lot

- **`.item()`, `.cpu()`, `.numpy()`, or a `print` of a tensor inside the loop** —
  each forces a synchronisation and stalls the pipeline. Accumulate on the GPU,
  transfer once per epoch.
- **Building tensors on CPU then moving them**, instead of
  `torch.zeros(..., device=device)`.
- **Python loops over batch elements.** Vectorise.
- **`F.softmax` before `CrossEntropyLoss`** — wrong *and* slower.
- **Validating every step** rather than every epoch.

## Before claiming a speedup

Measure with `torch.cuda.synchronize()` around a fixed number of steps, after a
warm-up, and report steps/second or samples/second. Also confirm the loss curve
is unchanged — a "speedup" that quietly changes the numerics is a regression.
