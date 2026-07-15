## train_plm_daemon_v2.py

import argparse
import json
import os
import time
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

class PLMDataset(Dataset):
    """Dataset for PLM training on engineered daemon features (v2)."""

    def __init__(self, data_path):
        self.features = []
        self.labels = []
        self.label_to_idx = {}
        self.idx_to_label = []
    
        with open(data_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(',')
                if len(parts) != 6:  # 5 features + 1 label
                    raise ValueError(
                        f"Line {line_num}: Expected 6 columns (5 features + label), got {len(parts)}"
                    )
            
                # Parse features
                try:
                    feature_vals = [float(x) for x in parts[:5]]
                except ValueError as e:
                    raise ValueError(f"Line {line_num}: Non-numeric feature in '{line}'") from e
            
                label = parts[5].strip()
                if label not in self.label_to_idx:
                    self.label_to_idx[label] = len(self.idx_to_label)
                    self.idx_to_label.append(label)
            
                self.features.append(feature_vals)
                self.labels.append(self.label_to_idx[label])
    
        if not self.features:
            raise ValueError("No valid data found")
    
        self.features = torch.tensor(self.features, dtype=torch.float32)
        self.labels = torch.tensor(self.labels, dtype=torch.long)
        self.vocab_size = len(self.idx_to_label)

    def __len__(self):
        return len(self.features)

    def __getitem__(self, idx):
        return self.features[idx], self.labels[idx]


class MicroLanguageModel(nn.Module):
    """Constrained feedforward network for PLM (matches infer_mlm.py architecture)."""

    def __init__(self, input_dim, vocab_size, hidden_dim=16):
        super().__init__()
        self.network = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, vocab_size)
        )
        # Stable initialization for legacy hardware
        for layer in self.network:
            if isinstance(layer, nn.Linear):
                nn.init.xavier_uniform_(layer.weight)
                nn.init.zeros_(layer.bias)

    def forward(self, x):
        return self.network(x)


def train_plm(
    data_path,
    output_path,
    hidden_dim=16,
    batch_size=32,
    learning_rate=0.01,
    epochs=100,
    device="cpu"
):
    """Train and save PLM model."""
    if device not in ["cpu", "cuda"]:
        raise ValueError(f"Device must be 'cpu' or 'cuda', got {device}")
    if device == "cuda" and not torch.cuda.is_available():
        print("WARNING: CUDA requested but not available. Falling back to CPU.")
        device = "cpu"

    # Load dataset
    print(f"Loading engineered features from {data_path}...")
    dataset = PLMDataset(data_path)
    # pin_memory only on CUDA (faster host->GPU copies; a no-op/waste on CPU).
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True,
                            pin_memory=(device == "cuda"))

    print(f"Vocabulary ({dataset.vocab_size} commands): {dataset.idx_to_label}")
    print(f"Dataset size: {len(dataset)} samples")

    # Initialize model
    model = MicroLanguageModel(
        input_dim=5,  # Fixed: 5 engineered features (v2)
        vocab_size=dataset.vocab_size,
        hidden_dim=hidden_dim
    ).to(device)

    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(model.parameters(), lr=learning_rate)

    # Training loop
    print(f"Starting training on {device}...")
    start_time = time.time()
    model.train()

    for epoch in range(epochs):
        epoch_loss = 0.0
        correct = 0
        total = 0
    
        for features, labels in dataloader:
            features = features.to(device)
            labels = labels.to(device)
        
            outputs = model(features)
            loss = criterion(outputs, labels)
        
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        
            epoch_loss += loss.item()
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
    
        if (epoch + 1) % 10 == 0 or epoch == 0:
            avg_loss = epoch_loss / len(dataloader)
            accuracy = 100 * correct / total
            print(
                f"Epoch [{epoch+1}/{epochs}] "
                f"Loss: {avg_loss:.4f} "
                f"Acc: {accuracy:.2f}%"
            )

    training_time = time.time() - start_time
    print(f"Training completed in {training_time:.2f} seconds")
    print(f"Final accuracy: {100 * correct / total:.2f}%")

    # Save model and metadata
    print(f"Saving model to {output_path}...")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # nosemgrep -- torch.save() SERIALIZES; it is not a deserialization/RCE vector.
    # The load-side risk is mitigated separately with weights_only=True (see
    # infer_plm_daemon_v2.py / audit_archives_personal_v2.py). Saved payload is
    # tensors + plain dicts/ints, so it stays weights_only-loadable downstream.
    torch.save({
        'model_state_dict': model.state_dict(),
        'idx_to_label': dataset.idx_to_label,
        'label_to_idx': dataset.label_to_idx,
        'feature_dim': 5,
        'hidden_dim': hidden_dim,
        'training_accuracy': 100 * correct / total,
        'training_time_seconds': training_time
    }, output_path)

    print("Model saved successfully!")


def main():
    parser = argparse.ArgumentParser(
        description="Train Personal Language Model (PLM) from daemon command sequence (v2)"
    )
    parser.add_argument(
        "--data",
        required=True,
        help="Path to engineered features CSV (5 features + label)"
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to save trained PLM model (.pt file)"
    )
    parser.add_argument(
        "--hidden",
        type=int,
        default=16,
        help="Hidden layer size (default: 16)"
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=32,
        help="Batch size (default: 32)"
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=0.01,
        help="Learning rate (default: 0.01)"
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=100,
        help="Number of training epochs (default: 100)"
    )
    parser.add_argument(
        "--device",
        choices=["cpu", "cuda"],
        default="cpu",
        help="Compute device (default: cpu)"
    )

    args = parser.parse_args()

    try:
        train_plm(
            data_path=args.data,
            output_path=args.output,
            hidden_dim=args.hidden,
            batch_size=args.batch,
            learning_rate=args.lr,
            epochs=args.epochs,
            device=args.device
        )
    except Exception as e:
        print(f"Training failed: {str(e)}")
        raise
        
        
if __name__ == "__main__":
    main()