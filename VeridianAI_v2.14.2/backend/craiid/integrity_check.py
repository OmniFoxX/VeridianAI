import json
import re
from pathlib import Path

def load_audit_insights():
    """Load insights from the audit summary text file."""
    insights_path = Path("audit_insights_summary.txt")
    if not insights_path.is_file():
        # Try alternative location
        insights_path = Path("Downloads/audit_insights_summary.txt")
    if not insights_path.is_file():
        raise FileNotFoundError("audit_insights_summary.txt not found")
    content = insights_path.read_text(encoding="utf-8")
    return content

def load_personal_json():
    """Load the personal audit JSON."""
    json_path = Path("audit_personal_1000_Todd.json")
    if not json_path.is_file():
        json_path = Path("Downloads/audit_personal_1000_Todd.json")
    if not json_path.is_file():
        raise FileNotFoundError("audit_personal_1000_Todd.json not found")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return data

def run_integrity_check():
    """Run integrity checks based on audit insights."""
    checks_passed = []
    checks_failed = []
    
    # Load data
    try:
        insights = load_audit_insights()
        personal_data = load_personal_json()
    except Exception as e:
        return False, f"Failed to load audit data: {e}"
    
    # 1. Check user behavioral profile metrics
    try:
        profile = personal_data["user_behavioral_profile"]
        ttr = profile["lexical_richness_ttr"]
        entropy = profile["vocabulary_entropy_normalized"]
        avg_len = profile["avg_turn_length_words"]
        
        # Expect TTR > 0.03, entropy > 0.8, avg turn length > 400
        if ttr > 0.03:
            checks_passed.append(f"Lexical richness TTR={ttr:.4f} > 0.03")
        else:
            checks_failed.append(f"Lexical richness TTR={ttr:.4f} <= 0.03")
            
        if entropy > 0.8:
            checks_passed.append(f"Vocabulary entropy={entropy:.4f} > 0.8")
        else:
            checks_failed.append(f"Vocabulary entropy={entropy:.4f} <= 0.8")
            
        if avg_len > 400:
            checks_passed.append(f"Average turn length={avg_len:.1f} > 400 words")
        else:
            checks_failed.append(f"Average turn length={avg_len:.1f} <= 400 words")
    except KeyError as e:
        checks_failed.append(f"Missing key in user_behavioral_profile: {e}")
    
    # 2. Check drift analysis has three segments
    try:
        drift = personal_data["drift_analysis"]
        segments = drift.get("segments", [])
        if len(segments) == 3:
            checks_passed.append("Drift analysis contains exactly 3 chronological segments")
        else:
            checks_failed.append(f"Drift analysis has {len(segments)} segments, expected 3")
    except KeyError:
        checks_failed.append("Missing drift_analysis in personal data")
    
    # 3. Check personalization candidates include expected user-unique words
    expected_unique = {"ws", "wschat", "wiring", "workers", "worker", "witness", "wired", "windows"}
    try:
        unique_vocab = set(personal_data["personalization_candidates"]["user_unique_vocabulary"].keys())
        found = expected_unique & unique_vocab
        if len(found) >= 4:  # at least half of expected
            checks_passed.append(f"Found {len(found)}/{len(expected_unique)} expected user-unique words: {sorted(found)}")
        else:
            checks_failed.append(f"Only found {len(found)}/{len(expected_unique)} expected user-unique words. Missing: {expected_unique - unique_vocab}")
    except KeyError:
        checks_failed.append("Missing personalization_candidates.user_unique_vocabulary")
    
    # 4. Check vocabulary overlap is substantial
    # Extract from insights text
    overlap_match = re.search(r"Vocabulary overlap: (\d+) shared words", insights)
    if overlap_match:
        overlap = int(overlap_match.group(1))
        if overlap >= 3000:
            checks_passed.append(f"Vocabulary overlap={overlap} shared words >= 3000")
        else:
            checks_failed.append(f"Vocabulary overlap={overlap} shared words < 3000")
    else:
        checks_failed.append("Could not extract vocabulary overlap from insights summary")
    
    # 5. Check that global top words include expected domain terms
    expected_terms = {"chat", "file", "sage", "daemon", "memory", "audit", "tool", "craiid"}
    # We'll check the insights for presence of these terms (simple)
    missing_terms = []
    for term in expected_terms:
        if term not in insights.lower():
            missing_terms.append(term)
    if not missing_terms:
        checks_passed.append("All expected domain terms found in insights summary")
    else:
        checks_failed.append(f"Missing domain terms in insights: {missing_terms}")
    
    # Overall result
    if not checks_failed:
        return True, "All integrity checks passed.\n" + "\n".join(checks_passed)
    else:
        return False, f"{len(checks_failed)} check(s) failed:\n" + "\n".join(checks_failed) + "\n\nPassed checks:\n" + "\n".join(checks_passed)

if __name__ == "__main__":
    passed, message = run_integrity_check()
    print(message)
    exit(0 if passed else 1)