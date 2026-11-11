"""EfficientNet-B0 transfer learning, persisted TPE trials, holdout evaluation and ONNX export."""

import argparse
import csv
import hashlib
import json
import random
from pathlib import Path

import cv2
import numpy as np
import optuna
import torch
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, recall_score
from torch import nn
from torch.utils.data import DataLoader, Dataset
from torchvision.models import EfficientNet_B0_Weights, efficientnet_b0

from marine.vision import VERSION, enhance


def focal(logits, labels, alpha, gamma):
    logp = logits.float().log_softmax(dim=1).gather(1, labels[:, None]).squeeze(1)
    return (-alpha[labels] * (1 - logp.exp()).pow(gamma) * logp).mean()


class Images(Dataset):
    def __init__(self, rows, root, labels, mode, augment=False):
        self.rows, self.root, self.labels, self.mode = rows, Path(root), labels, mode
        self.augment = augment

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        row = self.rows[index]
        path = (self.root / row["path"]).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Image path escapes dataset")
        image = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)
        image = enhance(image, self.mode)
        if self.augment:
            image = np.clip(image.astype(float) * random.uniform(0.85, 1.15), 0, 255).astype(
                np.uint8
            )
            if random.random() < 0.5:
                image = image[:, ::-1].copy()
        value = torch.from_numpy(image.transpose(2, 0, 1).copy()).float() / 255
        value = (value - torch.tensor([0.485, 0.456, 0.406])[:, None, None]) / torch.tensor(
            [0.229, 0.224, 0.225]
        )[:, None, None]
        return value, self.labels.index(row["label"])


def evaluate(labels, probabilities):
    predictions = probabilities.argmax(axis=1)
    classes = list(range(probabilities.shape[1]))
    n = len(labels)
    accuracy = float(accuracy_score(labels, predictions))
    # Wilson interval; group correlation is separately disclosed in the report.
    z = 1.96
    center = (accuracy + z * z / (2 * n)) / (1 + z * z / n)
    half = z * np.sqrt(accuracy * (1 - accuracy) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return {
        "accuracy": accuracy,
        "accuracyWilson95": [center - half, center + half],
        "macroF1": float(
            f1_score(labels, predictions, labels=classes, average="macro", zero_division=0)
        ),
        "recallByClass": recall_score(
            labels, predictions, labels=classes, average=None, zero_division=0
        ).tolist(),
        "confusionMatrix": confusion_matrix(labels, predictions, labels=classes).tolist(),
        "sampleCount": n,
    }


def run(args):
    torch.set_num_threads(min(4, torch.get_num_threads()))
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "model.pt").exists() or (out / "model.onnx").exists():
        raise ValueError(
            "Successful or in-progress model artifact already exists. Reuse it; choose a new output directory for a new experiment."
        )
    rows = list(csv.DictReader(open(args.manifest)))
    labels = sorted({r["label"] for r in rows})
    if len(labels) < 2:
        raise ValueError("At least two classes are required")
    splits = {s: [r for r in rows if r["split"] == s] for s in ["train", "validation", "test"]}
    if any(not r for r in splits.values()):
        raise ValueError("All splits must be nonempty")
    group_sets = {s: {r["group"] for r in rs} for s, rs in splits.items()}
    if any(
        group_sets[a] & group_sets[b]
        for a, b in [("train", "validation"), ("train", "test"), ("validation", "test")]
    ):
        raise ValueError("Group leakage across splits")
    for name, rs in splits.items():
        if {r["label"] for r in rs} != set(labels):
            raise ValueError(f"Missing class in {name}")
    device = torch.device(
        args.device
        if args.device != "auto"
        else ("mps" if torch.backends.mps.is_available() else "cpu")
    )
    model = efficientnet_b0(weights=EfficientNet_B0_Weights.IMAGENET1K_V1)
    model.classifier = nn.Identity()
    model = model.to(device).eval()
    manifest_hash = hashlib.sha256(Path(args.manifest).read_bytes()).hexdigest()
    # Cache is tied to exact manifest, transform, and pretrained model identity.
    fingerprint = hashlib.sha256(
        (manifest_hash + args.mode + VERSION + torch.__version__).encode()
    ).hexdigest()
    cache = out / f"features-{fingerprint[:16]}.npz"
    if cache.exists():
        saved = np.load(cache, allow_pickle=False)
        features = {
            s: (torch.from_numpy(saved[s + "_x"]), torch.from_numpy(saved[s + "_y"]))
            for s in splits
        }
    else:
        features = {}
        for split, rs in splits.items():
            loader = DataLoader(
                Images(rs, args.root, labels, args.mode), batch_size=args.batch_size, num_workers=0
            )
            xs, ys = [], []
            with torch.inference_mode():
                for i, (x, y) in enumerate(loader):
                    xs.append(model(x.to(device)).cpu())
                    ys.append(y)
                    if i % 20 == 0:
                        print(
                            json.dumps(
                                {
                                    "phase": "embedding",
                                    "split": split,
                                    "batch": i,
                                    "total": len(loader),
                                }
                            ),
                            flush=True,
                        )
            features[split] = (torch.cat(xs), torch.cat(ys))
        np.savez_compressed(
            cache,
            **{
                f"{s}_{k}": value.numpy()
                for s, xy in features.items()
                for k, value in zip(["x", "y"], xy)
            },
        )
    # Frozen-backbone search is inexpensive and genuinely trains every candidate classifier.
    x, y = features["train"]
    vx, vy = features["validation"]
    counts = torch.bincount(y, minlength=len(labels)).float()

    def fit(config):
        torch.manual_seed(args.seed)
        head = nn.Sequential(nn.Dropout(config["dropout"]), nn.Linear(x.shape[1], len(labels)))
        optimizer = torch.optim.AdamW(
            head.parameters(), lr=config["lr"], weight_decay=config["weight_decay"]
        )
        alpha = counts.rsqrt() if config["alpha"] == "sqrt_inverse" else torch.ones_like(counts)
        alpha = alpha.clamp(max=alpha.min() * 5)
        alpha = alpha / alpha.mean()
        best, state = -1, None
        for _ in range(args.epochs):
            head.train()
            permutation = torch.randperm(len(y))
            for indices in permutation.split(128):
                optimizer.zero_grad()
                loss = focal(head(x[indices]), y[indices], alpha, config["gamma"])
                loss.backward()
                optimizer.step()
            head.eval()
            with torch.inference_mode():
                score = f1_score(vy.numpy(), head(vx).argmax(1).numpy(), average="macro")
            if score > best:
                best = score
                state = {k: v.detach().clone() for k, v in head.state_dict().items()}
        head.load_state_dict(state)
        return head, best

    def objective(trial):
        config = {
            "lr": trial.suggest_float("lr", 1e-4, 1e-2, log=True),
            "weight_decay": trial.suggest_float("weight_decay", 1e-6, 1e-2, log=True),
            "dropout": trial.suggest_float("dropout", 0.1, 0.4),
            "gamma": trial.suggest_categorical("gamma", [0, 1, 2, 3]),
            "alpha": trial.suggest_categorical("alpha", ["uniform", "sqrt_inverse"]),
        }
        _, score = fit(config)
        return score

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=args.seed, n_startup_trials=12),
        storage=f"sqlite:///{out / 'study.sqlite3'}",
        study_name=fingerprint,
        load_if_exists=True,
    )
    remaining = max(
        0, args.trials - sum(t.state == optuna.trial.TrialState.COMPLETE for t in study.trials)
    )
    study.optimize(objective, n_trials=remaining)
    head, _ = fit(study.best_params)
    baseline, _ = fit(
        {"lr": 0.001, "weight_decay": 0.0001, "dropout": 0.2, "gamma": 0, "alpha": "uniform"}
    )
    full = efficientnet_b0(weights=None)
    full.classifier = head
    full.load_state_dict({k: v.cpu() for k, v in model.state_dict().items()}, strict=False)
    full = full.to(device)
    best_score = study.best_value
    if args.finetune_epochs:
        for parameter in full.features[:-2].parameters():
            parameter.requires_grad = False
        optimizer = torch.optim.AdamW(
            [p for p in full.parameters() if p.requires_grad],
            lr=3e-5,
            weight_decay=study.best_params["weight_decay"],
        )
        alpha = (
            counts.rsqrt()
            if study.best_params["alpha"] == "sqrt_inverse"
            else torch.ones_like(counts)
        )
        alpha = alpha.clamp(max=alpha.min() * 5)
        alpha = (alpha / alpha.mean()).to(device)
        loaders = {
            s: DataLoader(
                Images(rs, args.root, labels, args.mode, augment=s == "train"),
                batch_size=args.batch_size,
                shuffle=s == "train",
                num_workers=0,
            )
            for s, rs in splits.items()
        }
        best_state = {k: v.detach().cpu().clone() for k, v in full.state_dict().items()}
        for epoch in range(args.finetune_epochs):
            full.train()
            full.features[:-2].eval()
            for images, targets in loaders["train"]:
                optimizer.zero_grad()
                loss = focal(
                    full(images.to(device)), targets.to(device), alpha, study.best_params["gamma"]
                )
                loss.backward()
                optimizer.step()
            full.eval()
            actual, predicted = [], []
            with torch.inference_mode():
                for images, targets in loaders["validation"]:
                    actual.extend(targets.tolist())
                    predicted.extend(full(images.to(device)).argmax(1).cpu().tolist())
            score = f1_score(actual, predicted, average="macro")
            if score > best_score:
                best_score = score
                best_state = {k: v.detach().cpu().clone() for k, v in full.state_dict().items()}
            print(
                json.dumps({"phase": "finetune", "epoch": epoch + 1, "validationMacroF1": score}),
                flush=True,
            )
        full.load_state_dict(best_state)
    tx, ty = features["test"]
    with torch.inference_mode():
        full.eval()
        test_loader = DataLoader(
            Images(splits["test"], args.root, labels, args.mode),
            batch_size=args.batch_size,
            num_workers=0,
        )
        probs = torch.cat(
            [full(images.to(device)).softmax(1).cpu() for images, _ in test_loader]
        ).numpy()
        base_probs = baseline.eval()(tx).softmax(1).numpy()
    report = {
        "datasetManifestSha256": manifest_hash,
        "labels": labels,
        "device": str(device),
        "preprocessing": args.mode,
        "preprocessVersion": VERSION,
        "method": "ImageNet B0 frozen-head TPE search, then optional last-two-stage fine tuning",
        "finetuneEpochs": args.finetune_epochs,
        "trialsCompleted": sum(t.state == optuna.trial.TrialState.COMPLETE for t in study.trials),
        "bestParams": study.best_params,
        "validationMacroF1": best_score,
        "test": evaluate(ty.numpy(), probs),
        "baselineSameEnhancedFeatures": evaluate(ty.numpy(), base_probs),
        "counts": {s: len(rs) for s, rs in splits.items()},
        "seed": args.seed,
        "limitations": [
            "Public dataset only; not local sea-area acceptance",
            "Grouped by available filename metadata; not verified individual-fish identities",
            "Frozen backbone search, not full-network Bayesian tuning",
            "Confidence is uncalibrated; human-review threshold is provisional",
        ],
    }
    error = 1 - report["baselineSameEnhancedFeatures"]["accuracy"]
    report["relativeErrorReduction"] = (
        None if error == 0 else (error - (1 - report["test"]["accuracy"])) / error
    )
    (out / "report.json").write_text(json.dumps(report, indent=2))
    with open(out / "test-predictions.csv", "w") as f:
        writer = csv.writer(f)
        writer.writerow(["path", "group", "actual", "predicted", "confidence"])
        for row, probabilities in zip(splits["test"], probs):
            writer.writerow(
                [
                    row["path"],
                    row["group"],
                    row["label"],
                    labels[int(probabilities.argmax())],
                    float(probabilities.max()),
                ]
            )
    full = full.cpu().eval()
    torch.save(
        {
            "state_dict": full.state_dict(),
            "labels": labels,
            "preprocessing": args.mode,
            "preprocessVersion": VERSION,
            "dropout": study.best_params["dropout"],
            "manifestSha256": manifest_hash,
        },
        out / "model.pt",
    )
    torch.onnx.export(
        full,
        torch.zeros(1, 3, 224, 224),
        str(out / "model.onnx"),
        input_names=["image"],
        output_names=["logits"],
        dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}},
        opset_version=17,
        dynamo=False,
    )
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", default="artifacts/models/norfisk")
    parser.add_argument("--trials", type=int, default=60)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--finetune-epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--mode", choices=["original", "clahe", "dehaze"], default="clahe")
    parser.add_argument("--device", default="auto")
    run(parser.parse_args())
