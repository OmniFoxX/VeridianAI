## infer_plm_daemon_v2.py

import argparse
import json
import os
import time
import torch
import torch.nn as nn

class MicroLanguageModel(nn.Module):
    """Must match architecture used in train_plm_daemon_v2.py"""

    def __init__(self, input_dim, vocab_size, hidden_dim=16):
        super().__init__()
        self.network = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, vocab_size)
        )

    def forward(self, x):
        return self.network(x)


def infer_plm(
    model_path,
    features,
    device="cpu",
    return_probabilities=False
):
    """
    Perform inference with trained PLM model (v2).
    
    Args:
        model_path: Path to saved .pt file from train_plm_daemon_v2.py
        features: List of 5 engineered feature values [same_as_prev, ...]
        device: "cpu" or "cuda"
        return_probabilities: If True, returns (command, probabilities_dict)

    Returns:
        Predicted command string or (command, probabilities_dict) tuple
    """
    # Validate device
    if device not in ["cpu", "cuda"]:
        raise ValueError(f"Device must be 'cpu' or 'cuda', got {device}")
    if device == "cuda" and not torch.cuda.is_available():
        print("WARNING: CUDA requested but not available. Falling back to CPU.")
        device = "cpu"

    # Load model checkpoint
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model file not found: {model_path}")

    try:
        checkpoint = torch.load(model_path, map_location=device)
    except Exception as e:
        raise RuntimeError(f"Failed to load model: {str(e)}")

    # Validate checkpoint
    required_keys = {'model_state_dict', 'idx_to_label', 'feature_dim', 'hidden_dim'}
    if not required_keys.issubset(checkpoint.keys()):
        missing = required_keys - checkpoint.keys()
        raise ValueError(
            f"Model checkpoint missing keys: {missing}. "
            f"Available: {list(checkpoint.keys())}"
        )

    # Extract vocabulary
    idx_to_label = checkpoint['idx_to_label']
    label_to_idx = {label: idx for idx, label in enumerate(idx_to_label)}
    vocab_size = len(idx_to_label)
    feature_dim = checkpoint['feature_dim']

    # Validate feature dimension
    if len(features) != feature_dim:
        raise ValueError(
            f"Expected {feature_dim} features, got {len(features)}. "
            f"Features: {features}"
        )

    # Initialize model
    model = MicroLanguageModel(
        input_dim=feature_dim,
        vocab_size=vocab_size,
        hidden_dim=checkpoint['hidden_dim']
    ).to(device)

    try:
        model.load_state_dict(checkpoint['model_state_dict'])
    except Exception as e:
        raise RuntimeError(f"Failed to load model state: {str(e)}")

    model.eval()

    # Process features
    features_tensor = torch.tensor([features], dtype=torch.float32).to(device)  # [1, 5]

    # Inference
    try:
        with torch.no_grad():
            logits = model(features_tensor)
            probabilities = torch.softmax(logits, dim=1)
            predicted_idx = torch.argmax(probabilities, dim=1)
        
            predicted_label = idx_to_label[predicted_idx.item()]
        
            # Safety check (should never fail if model/vocab match)
            if predicted_label not in label_to_idx:
                raise RuntimeError(
                    f"Model predicted OOV label '{predicted_label}'. "
                    f"Allowed labels: {idx_to_label}"
                )
        
            if return_probabilities:
                probs = probabilities[0].cpu().numpy().tolist()
                prob_dict = {label: prob for label, prob in zip(idx_to_label, probs)}
                return predicted_label, prob_dict
            else:
                return predicted_label
    except Exception as e:
        raise RuntimeError(f"Inference failed: {str(e)}")
    
    
def main():
    parser = argparse.ArgumentParser(
        description="Run inference with trained Personal Language Model (PLM) v2"
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Path to trained PLM model .pt file"
    )
    parser.add_argument(
        "--features",
        required=True,
        help="Comma-separated feature values (e.g., '0.0,0.3,0.2,0.1,0.4')"
    )
    parser.add_argument(
        "--device",
        choices=["cpu", "cuda"],
        default="cpu",
        help="Compute device (default: cpu)"
    )
    parser.add_argument(
        "--prob",
        action="store_true",
        help="Return probability distribution over vocabulary"
    )
    
    args = parser.parse_args()

    try:
        # Parse features
        feature_vals = [float(x.strip()) for x in args.features.split(',')]
    
        # Run inference
        result = infer_plm(
            model_path=args.model,
            features=feature_vals,
            device=args.device,
            return_probabilities=args.prob
        )
    
        if args.prob:
            command, probs = result
            print(f"Prediction: {command}")
            print("Probabilities:")
            for label, prob in sorted(probs.items(), key=lambda x: x[1], reverse=True):
                print(f"  {label}: {prob:.4f}")
        else:
            print(result)
        
    except Exception as e:
        print(f"Inference error: {str(e)}")
        raise
        
        
if __name__ == "__main__":
    main()