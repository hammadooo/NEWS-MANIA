"""Picks the N biggest, independent stories from the fresh articles.

Score = how many other outlets cover the same story (strongest signal)
      + severity keywords (violence, disasters, deaths, court/politics/markets)
      + prominence on the section page + a small freshness bonus.
Same story from several outlets is posted ONCE, with 'Also reported by ...'.
"""
import re
from datetime import datetime, timezone

STOP = set(
    "the a an of in on at to for and or with by from as is are was were be been after before over "
    "into amid says said say new news live updates update latest today India Indian".lower().split()
)

# crime / violence / brutality -> tagged 'crime' so topics can subscribe to it
VIOLENCE = re.compile(
    r"\b(kill\w*|murder\w*|dead|death|dies|died|shot|shoot\w*|stab\w*|rape\w*|assault\w*|molest\w*|"
    r"lynch\w*|blast|explosion|terror\w*|attack\w*|massacre|brutal\w*|abduct\w*|kidnap\w*|riot\w*|"
    r"clash\w*|encounter|custodial|acid|burnt|beaten|thrash\w*|bomb\w*|hostage|gunmen|militant\w*|"
    r"lathi\w*|tear ?gas|water ?cannon|detain\w*|baton|pellet|firing|brutality|crackdown|"
    r"crash\w*|collapse\w*|stampede|fire|flood\w*|earthquake|cyclone|landslide)\b",
    re.I,
)
BIG = re.compile(
    r"\b(breaking|just in|urgent|war|ceasefire|missile|strike\w*|supreme court|high court|"
    r"prime minister|modi|sensex|nifty|rbi|election\w*|resign\w*|arrest\w*|verdict|budget|"
    r"death toll|evacuat\w*|emergency|sanction\w*|protest\w*)\b",
    re.I,
)


def _tokens(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3 and w not in STOP}


def _same_story(ta, tb):
    inter = len(ta & tb)
    return inter >= 3 and inter / max(1, min(len(ta), len(tb))) >= 0.55


def tag(a):
    text = f"{a.title} {a.summary}"
    if VIOLENCE.search(text):
        a.tags.add("crime")
    return a


def pick_top(arts, n=10):
    """Returns (picked_articles_best_first, urls_covered_incl_duplicates)."""
    now = datetime.now(timezone.utc)
    toks = [_tokens(a.title) for a in arts]
    groups = []  # list of lists of indices
    for i, a in enumerate(arts):
        for g in groups:
            if _same_story(toks[i], toks[g[0]]):
                g.append(i)
                break
        else:
            groups.append([i])

    scored = []
    for g in groups:
        members = [arts[i] for i in g]
        sources = {m.source for m in members}
        # lead article: best listing rank, then earliest published
        lead = min(members, key=lambda m: (m.rank, m.published))
        tag(lead)
        text = f"{lead.title} {lead.summary}"
        age_min = (now - lead.published).total_seconds() / 60
        score = (
            4.0 * (len(sources) - 1)
            + (2.0 if VIOLENCE.search(text) else 0)
            + (2.0 if BIG.search(text) else 0)
            + max(0.0, 5.0 - 0.25 * lead.rank)
            + max(0.0, (60 - age_min) / 30)
        )
        lead.also = sorted(sources - {lead.source})
        scored.append((score, lead, members))

    scored.sort(key=lambda x: x[0], reverse=True)
    picked, covered = [], []
    for _, lead, members in scored[:n]:
        picked.append(lead)
        covered.extend(m.url for m in members)
    return picked, covered
