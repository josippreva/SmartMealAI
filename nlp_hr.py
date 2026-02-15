# nlp_hr.py
import re
import unicodedata
from typing import List

# Osnovne "stop" riječi za HR chat (nije savršeno, ali pomaže)
HR_STOP = {
    "i", "ili", "pa", "te", "a", "ali", "no",
    "u", "na", "za", "od", "do", "iz", "s", "sa", "kod", "pri",
    "mi", "meni", "ti", "tvoj", "moja", "moje", "moj", "molim", "daj",
    "što", "sta", "kako", "možeš", "mozes", "jel", "je", "li",
    "recept", "imam", "imas", "imaš", "želim", "zelim", "trebam",
    "ne", "da", "će", "ce", "bi",
}

_RE_KEEP = re.compile(r"[^\wčćđšž\s]+", re.IGNORECASE)

def strip_accents(s: str) -> str:
    """rajčica -> rajcica"""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def normalize_hr(s: str) -> str:
    """lower + remove punct (keep hr chars) + normalize spaces"""
    s = (s or "").strip().lower()
    s = _RE_KEEP.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# Heuristički HR stemmer (dovoljno dobar za sastojke/recepte)
_SUFFIXES = [
    "ovima", "evima",
    "anjima", "enjima",
    "ama", "ima",
    "ovima", "evima",
    "anja", "enje",
    "anje", "enje",
    "aciju", "acija", "acije", "aciji", "acijom",
    "ovima", "evima",
    "ovi", "eve",
    "ama", "ima",
    "anje", "enje",
    "om", "em", "am", "im",
    "u", "a", "e", "i", "o",
]

def stem_hr_token(tok: str) -> str:
    """
    piletinu/piletina/piletine -> piletin
    rajčica/rajčice/rajčicu -> rajcic
    """
    t = normalize_hr(tok)
    if not t or len(t) <= 2:
        return t

    # radi bez dijakritike radi robusnosti ("rajcica" i "rajčica" isto)
    t = strip_accents(t)

    # ne diraj kratke tokene
    if len(t) <= 4:
        return t

    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            t = t[: -len(suf)]
            break
    return t

def tokenize_hr(s: str) -> List[str]:
    s = normalize_hr(s)
    if not s:
        return []
    raw = s.split()
    toks: List[str] = []
    for w in raw:
        w0 = strip_accents(w)
        if w0 in HR_STOP:
            continue
        if len(w0) <= 1:
            continue
        toks.append(w)
    return toks

def tokens_stemmed(s: str) -> List[str]:
    return [stem_hr_token(t) for t in tokenize_hr(s)]

def extract_after_keyword(text_msg: str, keyword: str) -> str:
    t = normalize_hr(text_msg)
    if keyword not in t:
        return ""
    return t.split(keyword, 1)[1].strip()
