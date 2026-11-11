"""Deterministic, versioned OpenCV preprocessing shared by training and inference."""

import cv2
import numpy as np

VERSION = "opencv-light-v1"


def enhance(image: np.ndarray, mode: str = "clahe", size: int = 224) -> np.ndarray:
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("Expected an RGB image")
    if not 16 <= size <= 1024:
        raise ValueError("Invalid output size")
    if mode not in {"original", "clahe", "dehaze"}:
        raise ValueError("Unknown enhancement mode")
    image = image.astype(np.uint8, copy=False)
    if mode == "dehaze":
        # Conservative dark-channel candidate; deliberately bounded to avoid extreme amplification.
        f = image.astype(np.float32) / 255
        dark = cv2.erode(f.min(axis=2), np.ones((7, 7), np.uint8))
        air = np.maximum(np.percentile(f.reshape(-1, 3), 99.5, axis=0), 0.3)
        t = np.clip(1 - 0.65 * dark / max(float(air.max()), 0.01), 0.25, 1)
        t = cv2.GaussianBlur(t, (5, 5), 0)
        image = (np.clip((f - air) / t[..., None] + air, 0, 1) * 255).astype(np.uint8)
    if mode != "original":
        lab = cv2.cvtColor(image, cv2.COLOR_RGB2LAB)
        light = lab[..., 0]
        low, high = np.percentile(light, [1, 99])
        if high - low > 8:
            if mode == "dehaze":
                light = np.clip((light.astype(float) - low) * 255 / (high - low), 0, 255).astype(
                    np.uint8
                )
            lab[..., 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(light)
            image = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    h, w = image.shape[:2]
    ratio = size / max(h, w)
    resized = cv2.resize(image, (max(1, round(w * ratio)), max(1, round(h * ratio))))
    out = np.full((size, size, 3), 127, dtype=np.uint8)
    rh, rw = resized.shape[:2]
    out[(size - rh) // 2 : (size - rh) // 2 + rh, (size - rw) // 2 : (size - rw) // 2 + rw] = (
        resized
    )
    return out


def uncertainty_diversity(probabilities, embeddings, budget=100, seed=42):
    """Entropy prefilter + farthest point selection, with a 20% random sentinel budget."""
    p = np.asarray(probabilities, dtype=float)
    e = np.asarray(embeddings, dtype=float)
    if p.ndim != 2 or e.ndim != 2 or len(p) != len(e) or len(p) > 5000:
        raise ValueError("Expected matching bounded matrices")
    if not np.isfinite(p).all() or not np.isfinite(e).all() or (p < 0).any():
        raise ValueError("Invalid values")
    if not np.allclose(p.sum(axis=1), 1) or p.shape[1] < 2:
        raise ValueError("Expected multiclass probabilities")
    budget = min(max(int(budget), 0), len(p))
    if not budget:
        return []
    e = e / np.maximum(np.linalg.norm(e, axis=1, keepdims=True), 1e-9)
    entropy = -(p * np.log(np.maximum(p, 1e-9))).sum(axis=1)
    candidates = np.argsort(-entropy)[: min(len(p), budget * 5)].tolist()
    selected = [candidates.pop(0)]
    deterministic = max(1, round(budget * 0.8))
    distances = 1 - e @ e[selected[0]]
    while candidates and len(selected) < deterministic:
        choice = max(candidates, key=lambda i: distances[i])
        selected.append(choice)
        candidates.remove(choice)
        distances = np.minimum(distances, 1 - e @ e[choice])
    remaining = np.array(sorted(set(range(len(p))) - set(selected)))
    if budget > len(selected):
        selected.extend(
            np.random.default_rng(seed)
            .choice(remaining, budget - len(selected), replace=False)
            .tolist()
        )
    return selected
