# fuzzy_match.py
from typing import List, Optional, Tuple
from rapidfuzz import process, fuzz
from nlp_hr import stem_hr_token, strip_accents, normalize_hr

def _norm_key(s: str) -> str:
    # normalize + remove accents + stem
    return stem_hr_token(strip_accents(normalize_hr(s)))

def best_match(query: str, choices: List[str], score_cutoff: int = 80) -> Optional[Tuple[str, int]]:
    """
    Vraća (najbliži_choice, score) ili None.
    """
    if not query or not choices:
        return None

    qk = _norm_key(query)

    keyed = [(c, _norm_key(c)) for c in choices]
    keys = [k for _, k in keyed]

    hit = process.extractOne(qk, keys, scorer=fuzz.WRatio, score_cutoff=score_cutoff)
    if not hit:
        return None

    _, score, idx = hit
    return keyed[idx][0], int(score)
