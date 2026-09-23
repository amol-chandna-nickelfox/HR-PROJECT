from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parents[2] / ".env", override=True)

import re
import os
import json
import hashlib
from datetime import datetime

try:
    import anthropic as _anthropic
    _key = os.getenv("CLAUDE_API_KEY")
    claude_client = _anthropic.Anthropic(api_key=_key, timeout=60.0) if _key else None
except ImportError:
    claude_client = None

CLAUDE_MODEL = "claude-sonnet-4-6"

# Bump on any change to score_skills / the score weights so cached scores are recomputed.
_SCORER_VERSION = "4"


def _claude(system: str, prompt: str) -> str:
    resp = claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


SKILLS_DB = [
    "python", "java", "javascript", "typescript", "c++", "c#", "go", "rust",
    "ruby", "php", "scala", "kotlin", "swift", "r", "matlab", "bash", "shell",
    "machine learning", "deep learning", "nlp", "natural language processing",
    "computer vision", "reinforcement learning", "neural networks", "transformers",
    "bert", "gpt", "llm", "rag", "fine-tuning", "transfer learning",
    "tensorflow", "pytorch", "keras", "scikit-learn", "sklearn", "xgboost",
    "lightgbm", "hugging face", "spacy", "nltk", "opencv", "fastai",
    "sentence transformers", "faiss", "langchain",
    "fastapi", "flask", "django", "react", "angular", "vue", "node.js",
    "express", "spring boot", "rest api", "graphql",
    "aws", "azure", "gcp", "google cloud", "docker", "kubernetes", "jenkins",
    "ci/cd", "terraform", "ansible", "linux",
    "sql", "mysql", "postgresql", "mongodb", "redis", "sqlite",
    "elasticsearch", "cassandra", "nosql",
    "git", "airflow", "kafka", "spark", "hadoop", "tableau", "power bi",
    "pandas", "numpy", "matplotlib", "seaborn", "jupyter",
    "microservices", "agile", "scrum", "devops", "mlops",
]


def extract_email(text: str) -> str | None:
    match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', text)
    return match.group(0) if match else None


def validate_email(email: str | None) -> bool:
    if not email:
        return False
    return bool(re.match(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$', email))


def extract_phone(text: str) -> str | None:
    for pat in [r'\+?\d[\d\s\-().]{8,}\d']:
        for m in re.finditer(pat, text):
            digits = re.sub(r'\D', '', m.group(0))
            if 10 <= len(digits) <= 15:
                return m.group(0).strip()
    return None


def validate_phone(phone: str | None) -> bool:
    if not phone:
        return False
    digits = re.sub(r'\D', '', phone)
    return 10 <= len(digits) <= 15


def extract_name(text: str) -> str | None:
    skip_kw = {'resume', 'cv', 'curriculum', 'vitae', 'profile', 'summary',
               'skills', 'experience', 'education', 'contact'}
    for line in text.strip().splitlines()[:5]:
        cleaned = re.sub(r'[^\w\s]', '', line).strip()
        words = cleaned.split()
        if 2 <= len(words) <= 5 and not (set(w.lower() for w in words) & skip_kw):
            if all(w[0].isupper() for w in words if w):
                return cleaned
    return None


def extract_skills(text: str) -> list[str]:
    text_lower = text.lower()
    found = []
    for skill in SKILLS_DB:
        pattern = r'(?<!\w)' + re.escape(skill) + r'(?!\w)'
        if re.search(pattern, text_lower):
            found.append(skill.title() if len(skill) > 3 else skill.upper())
    return list(dict.fromkeys(found))


# Headings under which a document lists what it can do / what it wants.
_SKILL_SECTION_HEADER = re.compile(
    r'^\s*[*#>\-•·]*\s*(?:technical|key|core|professional|other|additional|relevant)?\s*'
    r'(?:skills?|competenc\w*|expertise|proficienc\w*|tools?|technolog\w*|'
    r'areas?\s+of\s+(?:expertise|knowledge)|requirements?|qualifications?|'
    r'must[\s\-]?haves?|good[\s\-]?to[\s\-]?haves?|nice[\s\-]?to[\s\-]?haves?|'
    r'what\s+we(?:\'re)?\s+looking\s+for|desired\s+\w+)'
    r'\s*[:\-–]?\s*(?P<inline>.*)$', re.I)

# Headings that mean the skills section has ended.
_SKILL_SECTION_STOP = re.compile(
    r'^\s*[*#>\-•·]*\s*(?:work\s+)?(?:experience|employment|education|academic\w*|project\w*|'
    r'certificat\w*|achievement\w*|award\w*|contact|summary|objective|profile|about|'
    r'personal\s+\w+|declaration|reference\w*|hobb\w*|interest\w*|language\w*|'
    r'responsibilit\w*|roles?\s+and\s+responsibilit\w*|benefits?|salary|compensation|'
    r'about\s+(?:us|the\s+company))\b', re.I)

_ITEM_SPLIT = re.compile(r'[,;|•·◦‣]|\s{3,}|\s+•\s+')
_MAX_SKILL_WORDS = 8

# A bulleted or numbered line is an item, never a heading. Without this the heading patterns
# above fire on bullet *content*: "- Proficiency in MS Office" was read as a "Proficiency:"
# heading, and "- Experience with MIS reports" as the start of the Experience section, which
# truncated a five-requirement JD to one item.
_BULLET_PREFIX = re.compile(r'^\s*(?:[\-–—*•·◦‣>]+|\d+[.)])\s+')


def _is_heading_like(line: str) -> bool:
    if _BULLET_PREFIX.match(line):
        return False
    return len(line.split(':', 1)[0].split()) <= 6


def _clean_skill_item(raw: str) -> str | None:
    s = re.sub(r'\*\*|__|`', '', raw or '').strip()
    s = re.sub(r'^[\-–—*•·◦‣>\s]+', '', s)
    s = re.sub(r'^\d+[.)]\s*', '', s)
    s = s.strip(' .:;-–—()[]')
    if not (2 <= len(s) <= 60):
        return None
    if len(s.split()) > _MAX_SKILL_WORDS:
        return None
    _h = _SKILL_SECTION_HEADER.match(s)
    if _h and not (_h.group('inline') or '').strip():
        return None                               # bare heading, e.g. "Key Skills"
    if len(s.split()) <= 2 and _SKILL_SECTION_STOP.match(s):
        return None                               # bare heading, e.g. "Work Experience"
    if not re.search(r'[A-Za-z]', s):
        return None
    if not _skill_tokens(s):                      # nothing but filler words
        return None
    return s


def extract_skills_generic(text: str) -> list[str]:
    """Skills a document actually names, without depending on SKILLS_DB.

    SKILLS_DB is a fixed list of ~100 engineering terms. Anything outside that vocabulary —
    an admin, finance, HR or operations role, a domain tool, or simply a newer framework —
    extracted as nothing, so the requirement could never be matched and the candidate was
    scored short of it. This reads whatever the document itself lists under a skills /
    requirements heading and unions that with the SKILLS_DB hits, so the known list is now a
    supplement rather than the ceiling.
    """
    found: list[str] = []
    lines = (text or "").splitlines()
    in_section = False
    blanks = 0

    for line in lines:
        stripped = line.strip()

        heading = _is_heading_like(stripped)

        if heading and _SKILL_SECTION_STOP.match(stripped):
            in_section = False
            continue

        header = _SKILL_SECTION_HEADER.match(stripped) if heading else None
        if header:
            in_section, blanks = True, 0
            tail = (header.group('inline') or '').strip()
            if tail:                                     # "Skills: Python, Excel, MIS"
                for part in _ITEM_SPLIT.split(tail):
                    item = _clean_skill_item(part)
                    if item:
                        found.append(item)
            continue

        if not in_section:
            continue
        if not stripped:
            blanks += 1
            if blanks >= 2:                              # blank run ends the section
                in_section = False
            continue
        blanks = 0

        for part in _ITEM_SPLIT.split(stripped):
            item = _clean_skill_item(part)
            if item:
                found.append(item)

    found.extend(extract_skills(text))                   # keep the curated tech vocabulary
    seen, out = set(), []
    for s in found:
        k = s.lower()
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out[:60]


def calculate_experience_years(text: str) -> str | None:
    month_map = {m: i+1 for i, m in enumerate(
        ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'])}
    now = datetime.now()
    total_months = 0
    p1 = (r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'
          r'\s+(\d{4})\s*[–\-—]+\s*'
          r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(\d{4})')
    p2 = (r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*'
          r'\s+(\d{4})\s*[–\-—]+\s*(present|current|now)')
    tl = text.lower()
    for m in re.finditer(p1, tl):
        s = month_map.get(m.group(1)[:3], 1), int(m.group(2))
        e = month_map.get(m.group(3)[:3], 1), int(m.group(4))
        total_months += max(0, (e[1]-s[1])*12 + (e[0]-s[0]))
    for m in re.finditer(p2, tl):
        s = month_map.get(m.group(1)[:3], 1), int(m.group(2))
        total_months += max(0, (now.year-s[1])*12 + (now.month-s[0]))
    if total_months:
        return f"~{total_months/12:.1f} years"
    m = re.search(r'(\d+)\+?\s*years?\s*(of\s*)?(experience|exp)', tl)
    if m:
        return f"{m.group(1)}+ years"
    return None


def extract_education(text: str) -> list[str]:
    edu_kw = ['b.tech','btech','b.e','be ','m.tech','mtech','mba',
              'b.sc','bsc','m.sc','msc','bachelor','master','phd',
              'ph.d','bca','mca','diploma','b.com']
    lines = text.splitlines()
    result = []
    for i, line in enumerate(lines):
        if any(kw in line.lower() for kw in edu_kw):
            entry = line.strip()
            if i+1 < len(lines):
                nxt = lines[i+1].strip()
                if nxt and not any(k in nxt.lower() for k in ['skill','experience','project','certification']):
                    entry += f"  |  {nxt}"
            result.append(entry)
    return result or None


def extract_projects(text: str) -> list[str]:
    titles = re.findall(r'\*\*([^*\n]{5,60})\*\*', text)
    section_kw = {'experience','skills','education','summary','certification',
                  'work','professional','contact','tool','framework'}
    return [t.strip() for t in titles
            if not any(k in t.lower() for k in section_kw)] or None


def extract_roles(text: str) -> list[str]:
    roles = []
    pattern = r'\*\*([A-Z][^\n*]{5,60})\*\*\s*\n([^\n]+\|[^\n]+)'
    for m in re.finditer(pattern, text):
        roles.append(f"{m.group(1).strip()}  —  {m.group(2).strip()}")
    return roles or None


def _exp_numeric(exp_str: str | None) -> float:
    if not exp_str:
        return 0.0
    m = re.search(r'(\d+\.?\d*)', exp_str)
    return float(m.group(1)) if m else 0.0


# Words carrying no domain signal — dropped before comparing, so "Advanced MS Excel" and
# "MS Excel" reduce to the same thing.
_SKILL_NOISE = {
    "a", "an", "the", "and", "or", "of", "in", "on", "to", "with", "for", "using", "use",
    "skill", "skills", "ability", "abilities", "knowledge", "proficiency", "proficient",
    "experience", "experienced", "strong", "good", "excellent", "basic", "advanced",
    "intermediate", "working", "hands", "expert", "expertise", "familiar", "familiarity",
    "understanding", "sound", "solid", "etc", "various", "related", "relevant", "e", "g",
    "up", "on", "off", "re", "as", "at", "by", "is", "it", "be", "per", "via", "any", "all",
    "both", "such", "into", "from", "that", "this", "their", "your", "our", "must", "should",
}

# Tokens that appear in half the phrases in this domain — present in almost any admin resume,
# so a match on one of these alone means nothing. A real match needs something distinctive.
_SKILL_COMMON = {
    "manag", "coordinat", "report", "prepar", "support", "handl", "process", "work", "team",
    "offic", "data", "inform", "system", "servic", "task", "activ", "provid", "maintain",
    "assist", "administr", "profession", "busi", "compani", "organ", "detail", "level",
}

# Traits a resume cannot evidence as a listed skill. Counting these as missing hard skills was
# unfairly deflating every candidate — one resume was marked short of "professionalism",
# "multitasking" and "communication skills" while the interview scored those very dimensions
# directly. They are reported separately and excluded from the skill-match denominator: the
# resume measures capability, the interview measures conduct.
_SOFT_REQUIREMENT_HINTS = {
    "communicat", "interperson", "multitask", "confidenti", "discreet", "discretion",
    "profession", "flexib", "adaptab", "proactiv", "initiativ", "independ", "priorit",
    "time management", "timely", "punctual", "integrity", "attitud", "attention to detail",
    "detail-orient", "detail orient", "teamwork", "collabor", "willing", "eager", "motivat",
    "reliabl", "trustworth", "etiquett", "presentabl", "pleasant", "polit", "courteous",
    "patien", "dedicat", "committ", "honest", "organizational", "organisational",
    "organization skill", "organisation skill", "well organ", "organized", "organised",
    "work under pressure", "self-start", "self start",
}

# Requirements naming a suite rather than a tool — satisfied by any member of the suite.
_SKILL_SUITES = {
    "microsoft office": {"word", "excel", "powerpoint", "outlook", "onenote", "office"},
    "ms office":        {"word", "excel", "powerpoint", "outlook", "onenote", "office"},
    "office suite":     {"word", "excel", "powerpoint", "outlook", "office"},
    "google workspace": {"sheet", "doc", "gmail", "slide", "workspac", "gsuit"},
    "g suite":          {"sheet", "doc", "gmail", "slide", "workspac", "gsuit"},
}


# (suffix, minimum characters that must remain), tried in order. "-ion" deliberately precedes
# "-ation" so the "-ate" verb family keeps a shared stem ("coordination" -> "coordinat", which
# also covers coordinate/coordinated/coordinator); stripping "-ation" first yielded "coordin"
# and split the family. "-ational" is absent for the opposite reason: it would collapse
# "international" onto "internal".
_STEM_SUFFIXES = (
    ("ions", 4), ("ion", 4), ("ations", 4), ("ation", 4),
    ("ities", 4), ("ity", 4), ("ments", 4), ("ment", 4),
    ("ings", 4), ("ing", 4),
    ("ances", 4), ("ance", 4), ("ences", 4), ("ence", 4),
    ("ives", 4), ("ive", 4), ("ials", 4), ("ial", 4),
    ("ors", 5), ("or", 5), ("ers", 5), ("er", 5),
    ("ies", 4), ("ied", 4), ("ed", 4), ("es", 4), ("al", 4), ("s", 4), ("y", 4),
)


def _stem(word: str) -> str:
    """Reduce inflected forms to a shared root so differently-worded skills compare equal.

    The previous version only stripped a handful of noun suffixes in a single pass and had no
    "-ed" rule at all, so a resume saying "managed the calendar", "scheduled meetings",
    "coordinated travel" or "handled correspondence" matched none of the JD duties that asked
    to "manage", "schedule", "coordinate" or "handle" them. It also stripped "-or" without a
    length guard, turning "vendor" into "vend" while "vendors" became "vendor" — a word that
    failed to match itself.

    Several passes run because real words stack suffixes ("presentations" -> "present",
    "confidentiality" -> "confidenti").
    """
    w = word
    for _ in range(3):
        for suf, min_rest in _STEM_SUFFIXES:
            if w.endswith(suf) and len(w) - len(suf) >= min_rest:
                w = w[: -len(suf)]
                break
        else:
            break
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    # A trailing "at" is left behind by "-ation"/"-ate" and its presence depends on the verb,
    # not the meaning: "preparation" reduces to "preparat" while "prepare" reduces to "prepar".
    # Dropping it unifies prepare/preparation, present/presentation, organize/organization and
    # coordinate/coordination. Known cost: "international" and "internal" both become "intern",
    # so a resume mentioning one can partially credit a requirement naming the other.
    if len(w) > 6 and w.endswith("at"):
        w = w[:-2]
    return w


def _skill_tokens(text: str) -> set[str]:
    """Meaningful stemmed tokens of a skill phrase, with MS/Microsoft normalised."""
    t = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
    t = re.sub(r"\bms\b", "microsoft", t)
    t = re.sub(r"is(e|ed|es|ing|ation|ations|ational)\b", r"iz\1", t)
    return {_stem(w) for w in t.split() if w and w not in _SKILL_NOISE}


def _is_soft_requirement(req: str) -> bool:
    return any(h in (req or "").lower() for h in _SOFT_REQUIREMENT_HINTS)


def _evidenced(requirement: str, haystack: set[str]) -> bool:
    """True when `haystack` (stemmed tokens of a resume) evidences `requirement`.

    Shared by every scoring dimension so "MIS Reporting" satisfies "mis reports preparation"
    consistently, whether the phrase came from a skills list, a responsibility bullet or a
    degree line.
    """
    req_l = (requirement or "").strip().lower()
    if not req_l:
        return False
    for suite, members in _SKILL_SUITES.items():          # "ms office" -> word/excel/...
        if suite in req_l and any(m in tok for m in members for tok in haystack):
            return True
    toks = _skill_tokens(req_l)
    if not toks:
        return False
    present = {t for t in toks if t in haystack}
    if present == toks:                                   # every meaningful token evidenced
        return True
    if not present:
        return False
    # A partial match only counts when something distinctive lines up — otherwise
    # "calendar management" would match any resume that merely says "management".
    distinctive = {t for t in present if t not in _SKILL_COMMON}
    return bool(distinctive) and len(present) / len(toks) >= 0.5


def _resume_haystack(resume_text: str, resume_skills=None) -> set[str]:
    haystack = set()
    for s in resume_skills or []:
        haystack |= _skill_tokens(s)
    return haystack | _skill_tokens(resume_text or "")


def score_skills(resume_skills, required_skills, good_to_have_skills=None, resume_text=""):
    """Match JD requirements against a resume by meaning rather than exact spelling.

    The previous version compared raw substrings, so anything phrased differently was scored as
    absent: a resume listing "MIS Reporting" and "Dashboard Preparation" was marked short of
    "mis reports preparation"; one listing "MS Word", "Advanced MS Excel" and "PowerPoint" was
    marked short of "ms office". Requirements are now compared as normalised, stemmed token
    sets, suite names are expanded to their members, and the whole resume — not only the
    extracted skills list — is searched, so a skill evidenced in an experience bullet counts.
    """
    good_to_have_skills = good_to_have_skills or []

    # Everything the resume evidences: the skills list plus the prose, since a requirement may
    # only show up in a job description bullet.
    haystack = _resume_haystack(resume_text, resume_skills)

    def _hit(req: str) -> bool:
        return _evidenced(req, haystack)

    def _split(skill_list):
        hard_hit, hard_miss, soft = [], [], []
        for raw in skill_list:
            s = (raw or "").strip()
            if not s:
                continue
            if _is_soft_requirement(s):
                soft.append(s.lower())
            elif _hit(s):
                hard_hit.append(s.lower())
            else:
                hard_miss.append(s.lower())
        return hard_hit, hard_miss, soft

    req_match, req_miss, req_soft = _split(required_skills or [])
    opt_match, opt_miss, opt_soft = _split(good_to_have_skills or [])

    # Denominators exclude soft traits, so a candidate is measured only on what a resume can
    # actually show.
    req_total = len(req_match) + len(req_miss)
    opt_total = len(opt_match) + len(opt_miss)
    req_score = round((len(req_match) / req_total) * 32) if req_total else 16
    opt_score = round((len(opt_match) / opt_total) * 8) if opt_total else 4
    raw = min(40, req_score + opt_score)

    return (
        raw,
        list(dict.fromkeys(req_match + opt_match)),
        req_miss + opt_miss,
        list(dict.fromkeys(req_soft + opt_soft)),
    )


_EXP_LEVEL_RANGES = {
    'Fresher (0-1 years)':   (0, 1),
    'Junior (1-3 years)':    (1, 3),
    'Mid-level (3-5 years)': (3, 5),
    'Senior (5+ years)':     (5, 99),
}

def _jd_years_required(jd_text: str) -> tuple[float, float] | None:
    """Years of experience a JD asks for, as (low, high).

    Only an explicit "N - M years" range was recognised before, so the very common
    "minimum 3 years", "3+ years" and "at least 3 years" phrasings all fell through to a flat
    score for every candidate.
    """
    tl = (jd_text or "").lower()
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:[–\-—]|to)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)', tl)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r'(?:minimum|min\.?|at\s+least|over|more\s+than|atleast)\s*'
                  r'(?:of\s+)?(\d+(?:\.\d+)?)\s*(?:years?|yrs?)', tl)
    if m:
        lo = float(m.group(1))
        return lo, lo + 4
    m = re.search(r'(\d+(?:\.\d+)?)\s*\+\s*(?:years?|yrs?)', tl)
    if m:
        lo = float(m.group(1))
        return lo, lo + 4
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\s*(?:of\s+)?(?:relevant\s+|work\s+|total\s+)?'
                  r'(?:experience|exp)', tl)
    if m:
        lo = float(m.group(1))
        return lo, lo + 4
    return None


def _grade_experience(candidate: float, lo: float, hi: float) -> int:
    if lo <= candidate <= hi:
        return 30
    if candidate > hi:
        return 25                                  # overqualified, still capable
    if candidate >= lo - 1:
        return 20
    if lo <= 0:
        return 20
    return max(8, round(30 * max(0.0, candidate) / lo))


def score_experience(exp_str, jd_text, experience_level=None):
    candidate = _exp_numeric(exp_str)
    if experience_level and experience_level in _EXP_LEVEL_RANGES:
        lo, hi = _EXP_LEVEL_RANGES[experience_level]
        return _grade_experience(candidate, lo, hi)

    rng = _jd_years_required(jd_text)
    if rng:
        return _grade_experience(candidate, *rng)

    # No stated requirement — grade on the candidate's own experience instead of handing every
    # applicant the same 22/30, which made a 6-year and a 1-year candidate indistinguishable.
    if candidate >= 5:
        return 28
    if candidate >= 3:
        return 25
    if candidate >= 1.5:
        return 21
    if candidate > 0:
        return 16
    return 12


# --- degree families, so a qualification is recognised regardless of discipline -------------
_DOCTORAL_KEYWORDS  = ['phd', 'ph.d', 'doctorate', 'doctoral']
_MASTERS_KEYWORDS   = ['m.tech', 'mtech', 'm.e.', 'mba', 'm.com', 'mcom', 'mca', 'm.sc', 'msc',
                       'm.a.', 'ma in', 'master', 'pgdm', 'pgdba', 'llm', 'm.ed', 'mph', 'mds']
_BACHELORS_KEYWORDS = ['b.tech', 'btech', 'b.e.', 'be in', 'b.com', 'bcom', 'bba', 'bca',
                       'b.sc', 'bsc', 'b.a.', 'ba in', 'bachelor', 'llb', 'b.ed', 'bms',
                       'bbm', 'b.arch', 'mbbs', 'b.pharm', 'graduate', 'graduation']
_DIPLOMA_KEYWORDS   = ['diploma', 'polytechnic', 'iti', 'certificate course', 'higher secondary',
                       '12th', 'intermediate']

# Lines a JD uses to state what the person will do. Kept separate from the skills list: a duty
# is a sentence ("prepare and circulate MIS reports weekly"), a skill is a noun phrase.
_JD_DUTY_HEADER = re.compile(
    r'^\s*[*#>\-•·]*\s*(?:key\s+|core\s+|main\s+|primary\s+)?'
    r'(?:responsibilit\w*|duties|role\s+and\s+responsibilit\w*|'
    r'roles?\s*(?:&|and)\s*responsibilit\w*|job\s+description|what\s+you[^\s]*\s+do|'
    r'day[\s\-]to[\s\-]day|scope\s+of\s+work|deliverables?)\s*[:\-–]?\s*(?P<inline>.*)$',
    re.I)

_JD_DUTY_STOP = re.compile(
    r'^\s*[*#>\-•·]*\s*(?:requirements?|qualifications?|skills?|experience|education|'
    r'benefits?|salary|compensation|about\s+(?:us|the\s+company)|how\s+to\s+apply|'
    r'perks?|location|employment\s+type|shift|working\s+hours?)\b', re.I)

_EDU_FIELD_PATTERN = re.compile(
    r'\b(?:degree|bachelors?|masters?|diploma|b\.?tech|b\.?com|b\.?sc|b\.?a\b|bba|bca|'
    r'mba|mca|m\.?com|m\.?sc|post\s*graduation)'
    r'(?:\'s)?\s*(?:degree\s*)?(?:in|of)\s+(?P<field>[A-Za-z][A-Za-z&/,\s]{2,60})', re.I)


def _jd_duties(jd_text: str) -> list[str]:
    """Responsibility lines from a JD; falls back to any bulleted line if unlabelled."""
    duties, in_section, blanks = [], False, 0
    for line in (jd_text or "").splitlines():
        stripped = line.strip()
        heading = _is_heading_like(stripped)
        if heading and _JD_DUTY_STOP.match(stripped):
            in_section = False
            continue
        header = _JD_DUTY_HEADER.match(stripped) if heading else None
        if header:
            in_section, blanks = True, 0
            tail = (header.group('inline') or '').strip()
            if tail:
                duties.append(tail)
            continue
        if not in_section:
            continue
        if not stripped:
            blanks += 1
            if blanks >= 2:
                in_section = False
            continue
        blanks = 0
        cleaned = re.sub(r'^[\-–—*•·◦‣>\s]+|^\d+[.)]\s*', '', stripped).strip(' .;')
        if cleaned and _skill_tokens(cleaned):
            duties.append(cleaned)

    if not duties:
        # Unlabelled JD — treat its bullets as the duty list rather than scoring everyone alike.
        for line in (jd_text or "").splitlines():
            stripped = line.strip()
            if not _BULLET_PREFIX.match(stripped):
                continue
            cleaned = re.sub(r'^[\-–—*•·◦‣>\s]+|^\d+[.)]\s*', '', stripped).strip(' .;')
            if cleaned and len(cleaned.split()) >= 2 and _skill_tokens(cleaned):
                duties.append(cleaned)
    return duties[:25]


def _jd_education_fields(jd_text: str) -> list[str]:
    """Fields of study a JD names, e.g. 'Commerce' from 'Bachelor's degree in Commerce'."""
    out = []
    for m in _EDU_FIELD_PATTERN.finditer(jd_text or ""):
        alternatives = re.split(r'\b(?:or|and|with|from|preferred|required|any)\b',
                                m.group('field'))
        for field in alternatives:
            for part in re.split(r'[,/&]', field):
                part = part.strip()
                if 2 < len(part) <= 40 and _skill_tokens(part):
                    out.append(part)
    return list(dict.fromkeys(out))[:8]


def score_projects(resume_text, jd_text):
    """How much of what the JD asks the person to *do* is evidenced in the resume.

    This used a fixed list of ML/software keywords ('nlp', 'chatbot', 'ocr', 'sentiment', ...).
    On any JD outside that domain none of them appeared, the ratio branch never ran, and every
    candidate — including an empty resume — received the same flat 14/20. The JD's own
    responsibility lines are the requirement list now.

    Each duty is scored by how much of its *distinctive* vocabulary the resume evidences, not
    as a hit/miss. Treating a duty as all-or-nothing punished verbose JDs: a twelve-word line
    like "Build and optimize RESTful APIs and microservices that power our products" almost
    never clears a whole-phrase bar, so a well-qualified engineer scored the same floor as an
    unrelated resume.
    """
    duties = _jd_duties(jd_text)
    if not duties:
        return 14                                  # nothing stated to measure against
    haystack = _resume_haystack(resume_text)

    coverage = []
    for duty in duties:
        toks = {t for t in _skill_tokens(duty) if t not in _SKILL_COMMON}
        if not toks:
            continue                               # filler line, carries no signal
        coverage.append(len(toks & haystack) / len(toks))
    if not coverage:
        return 14

    avg = sum(coverage) / len(coverage)
    # Full marks at 55% average coverage: no real resume restates a JD's whole vocabulary, so
    # demanding 100% would compress every candidate into the bottom of the range.
    return max(4, min(20, round((avg / 0.55) * 20)))


def score_education(resume_text, jd_text=""):
    """Degree level, plus field relevance judged against the JD rather than a fixed list.

    The old version only knew engineering degrees and only awarded the field bonus for
    computer science / IT / electronics / maths / statistics. A B.Com, BBA or MBA was not even
    recognised as a degree, so an MBA scored 5/10 — identical to no degree at all — on a role
    that asks for exactly that background.
    """
    tl = (resume_text or "").lower()

    if any(k in tl for k in _DOCTORAL_KEYWORDS):
        level = 10
    elif any(k in tl for k in _MASTERS_KEYWORDS):
        level = 9
    elif any(k in tl for k in _BACHELORS_KEYWORDS):
        level = 7
    elif any(k in tl for k in _DIPLOMA_KEYWORDS):
        level = 5
    else:
        return 5                                   # nothing recognisable as a qualification

    if not jd_text:
        return level
    # Field relevance: does any field the JD names show up alongside the qualification?
    jd_fields = _jd_education_fields(jd_text)
    if not jd_fields:
        return level
    haystack = _skill_tokens(resume_text)
    if any(_evidenced(f, haystack) for f in jd_fields):
        return 10
    return max(5, level - 2)


def get_experience_fit(candidate_exp, jd_text):
    m = re.search(r'(\d+)\s*[–\-—to]+\s*(\d+)\s*years?', jd_text.lower())
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if candidate_exp >= lo:
            return "Good"
        if candidate_exp >= lo - 1:
            return "Average"
        return "Poor"
    if candidate_exp >= 1:
        return "Good"
    return "Average"


def generate_reason(name, score, matching, missing, fit, exp):
    strength = "strong" if score >= 85 else ("good" if score >= 70 else "partial")
    verdict  = "highly recommended" if score >= 85 else ("recommended" if score >= 70 else "consider with reservations")
    m_str = ", ".join(matching[:4]) if matching else "relevant skills"
    x_str = ", ".join(missing[:3])  if missing  else "none significant"
    return (
        f"{name or 'The candidate'} shows a {strength} match with core skills "
        f"including {m_str}. "
        f"Experience fit is {fit.lower()} ({exp or 'N/A'} of experience). "
        f"Minor gaps: {x_str}. Overall verdict: {verdict}."
    )


def _strip_markdown_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw.rstrip())
    return raw.strip()


def _grok_parse(resume_text: str) -> dict:
    prompt = f"""Parse the resume below and return a JSON object with exactly these keys:
- name: full name string or null
- email: email string or null
- phone: phone number string or null
- skills: array of technical skill strings (be thorough)
- experience_years: total experience like "~3.5 years" or null
- education: array of education entry strings or null
- projects: array of project name strings or null
- roles: array of strings like "Job Title  —  Company | Start – End" or null

Return valid JSON only, no markdown, no extra text.

RESUME:
{resume_text}"""

    raw = _claude("You are an expert resume parser. Return only valid JSON.", prompt)
    return json.loads(_strip_markdown_json(raw))


def _extract_jd_skills(jd_text: str) -> dict:
    prompt = f"""From this job description, extract skills into two categories:
- required_skills: skills listed as required, must-have, or core technical competencies
- good_to_have_skills: skills listed as nice-to-have, good to have, preferred, or optional

Return JSON with exactly these two keys, each an array of lowercase strings. No markdown, no extra text.
If the JD does not distinguish, put all skills in required_skills and leave good_to_have_skills empty.

JOB DESCRIPTION:
{jd_text}"""
    raw = _claude("You are a technical recruiter. Return only valid JSON.", prompt)
    result = json.loads(_strip_markdown_json(raw))
    # Normalise: if Claude returns a plain list (legacy), treat as required
    if isinstance(result, list):
        return {"required_skills": result, "good_to_have_skills": []}
    return result


def _grok_analyze(resume_text: str, jd_text: str, jd_fields: dict = None) -> dict:
    parsed = _grok_parse(resume_text)

    if jd_fields:
        required_skills  = [s.strip() for s in jd_fields.get('skills', '').split(',') if s.strip()]
        good_to_have     = [s.strip() for s in jd_fields.get('goodToHave', '').split(',') if s.strip()]
        experience_level = jd_fields.get('experienceLevel')
    else:
        try:
            skills_data      = _extract_jd_skills(jd_text)
            required_skills  = skills_data.get('required_skills', [])
            good_to_have     = skills_data.get('good_to_have_skills', [])
        except Exception:
            required_skills  = extract_skills_generic(jd_text)
            good_to_have     = []
        experience_level = None

    resume_skills = parsed.get("skills") or []
    # Union with what the resume literally lists: Claude sometimes condenses or renames a
    # skill, and the JD may ask for it in the resume's own wording.
    _seen_rs = {s.lower() for s in resume_skills}
    for s in extract_skills_generic(resume_text):
        if s.lower() not in _seen_rs:
            _seen_rs.add(s.lower())
            resume_skills.append(s)

    s_skill, matching_skills, missing_skills, soft_reqs = score_skills(
        resume_skills, required_skills, good_to_have, resume_text=resume_text
    )
    exp_years = parsed.get("experience_years")
    s_exp  = score_experience(exp_years, jd_text, experience_level)
    s_proj = score_projects(resume_text, jd_text)
    s_edu  = score_education(resume_text, jd_text)
    total  = min(100, s_skill + s_exp + s_proj + s_edu)

    candidate_exp = _exp_numeric(exp_years)
    exp_fit = get_experience_fit(candidate_exp, jd_text)
    reason  = generate_reason(
        parsed.get("name"), total, matching_skills, missing_skills, exp_fit, exp_years
    )

    email_ok = validate_email(parsed.get("email"))
    phone_ok = validate_phone(parsed.get("phone"))

    return {
        "name":             parsed.get("name"),
        "email":            parsed.get("email") if email_ok else None,
        "phone":            parsed.get("phone") if phone_ok else None,
        "skills":           resume_skills,
        "experience_years": exp_years,
        "match_score":      f"{total} / 100",
        "matching_skills":  matching_skills,
        "missing_skills":   missing_skills,
        "soft_requirements": soft_reqs,
        "experience_fit":   exp_fit,
        "reason":           reason,
        "education":        parsed.get("education"),
        "projects":         parsed.get("projects"),
        "roles":            parsed.get("roles"),
        "email_valid":      email_ok,
        "phone_valid":      phone_ok,
        "score_breakdown": {
            "skill_match":          {"score": s_skill, "max": 40, "weight": "40%"},
            "experience_relevance": {"score": s_exp,   "max": 30, "weight": "30%"},
            "project_relevance":    {"score": s_proj,  "max": 20, "weight": "20%"},
            "education":            {"score": s_edu,   "max": 10, "weight": "10%"},
        },
    }


def parse_resume(resume_text: str) -> dict:
    if claude_client:
        try:
            return _grok_parse(resume_text)
        except Exception as e:
            print(f"[Grok parse fallback] {e}")

    return {
        "name":             extract_name(resume_text),
        "email":            extract_email(resume_text),
        "phone":            extract_phone(resume_text),
        "skills":           extract_skills_generic(resume_text),
        "experience_years": calculate_experience_years(resume_text),
        "education":        extract_education(resume_text),
        "projects":         extract_projects(resume_text),
        "roles":            extract_roles(resume_text),
    }


_analyze_cache: dict = {}


def analyze(resume_text: str, jd_text: str, jd_fields: dict = None) -> dict:
    # _SCORER_VERSION is part of the key on purpose: the cache below is permanent, so without
    # it every resume already analysed would keep returning a score produced by the old matching
    # logic forever. Bump it whenever scoring changes so results are re-computed once.
    cache_key = hashlib.md5(
        (_SCORER_VERSION + "|" + resume_text + jd_text + str(jd_fields or {})).encode()
    ).hexdigest()
    if cache_key in _analyze_cache:
        print(f"[Analyzer] cache hit (memory) {cache_key[:8]}")
        return _analyze_cache[cache_key]

    # Scoring is an LLM call and therefore not perfectly reproducible, even at temperature=0:
    # the same resume against the same JD scored 51 on one run and 55 on another (skill_match
    # 5 vs 9, 1 matching skill found vs 3). The in-memory cache above was the only guard and it
    # is lost on every restart, so identical inputs re-scored differently — which reads as a
    # broken system. The DB-backed cache makes the result stable for good.
    from backend.app.database import _get_cached_analysis, _set_cached_analysis
    _persisted = _get_cached_analysis(cache_key)
    if _persisted:
        print(f"[Analyzer] cache hit (db) {cache_key[:8]} — reusing the earlier score for identical inputs")
        _analyze_cache[cache_key] = _persisted
        return _persisted

    if claude_client:
        try:
            result = _grok_analyze(resume_text, jd_text, jd_fields)
            _analyze_cache[cache_key] = result
            _set_cached_analysis(cache_key, result)
            return result
        except Exception as e:
            print(f"[Grok analyze fallback] {e}")

    parsed = parse_resume(resume_text)
    resume_skills = parsed["skills"]
    exp_years     = parsed["experience_years"]
    candidate_exp = _exp_numeric(exp_years)

    if jd_fields:
        required_skills  = [s.strip() for s in jd_fields.get('skills', '').split(',') if s.strip()]
        good_to_have     = [s.strip() for s in jd_fields.get('goodToHave', '').split(',') if s.strip()]
        experience_level = jd_fields.get('experienceLevel')
    else:
        required_skills  = extract_skills_generic(jd_text)
        good_to_have     = []
        experience_level = None

    s_skill, matching_skills, missing_skills, soft_reqs = score_skills(
        resume_skills, required_skills, good_to_have, resume_text=resume_text
    )
    s_exp  = score_experience(exp_years, jd_text, experience_level)
    s_proj = score_projects(resume_text, jd_text)
    s_edu  = score_education(resume_text, jd_text)
    total  = min(100, s_skill + s_exp + s_proj + s_edu)

    exp_fit = get_experience_fit(candidate_exp, jd_text)
    reason  = generate_reason(
        parsed["name"], total, matching_skills, missing_skills, exp_fit, exp_years
    )
    email_ok = validate_email(parsed["email"])
    phone_ok = validate_phone(parsed["phone"])

    return {
        "name":             parsed["name"],
        "email":            parsed["email"] if email_ok else None,
        "phone":            parsed["phone"] if phone_ok else None,
        "skills":           resume_skills,
        "experience_years": exp_years,
        "match_score":      f"{total} / 100",
        "matching_skills":  matching_skills,
        "missing_skills":   missing_skills,
        "soft_requirements": soft_reqs,
        "experience_fit":   exp_fit,
        "reason":           reason,
        "education":        parsed["education"],
        "projects":         parsed["projects"],
        "roles":            parsed["roles"],
        "email_valid":      email_ok,
        "phone_valid":      phone_ok,
        "score_breakdown": {
            "skill_match":          {"score": s_skill, "max": 40, "weight": "40%"},
            "experience_relevance": {"score": s_exp,   "max": 30, "weight": "30%"},
            "project_relevance":    {"score": s_proj,  "max": 20, "weight": "20%"},
            "education":            {"score": s_edu,   "max": 10, "weight": "10%"},
        },
    }
