"""The block categories that can be switched on, for the web UI Categories
page and for the downloader.

The catalogue below is UT1's list (descriptions paraphrased from the README
of https://github.com/olbat/ut1-blacklists). Left out on purpose:
  * aliases of a category already listed (porn = adult, drugs = drogue,
    aggressive/violence = agressif, proxy = redirector, ads = publicite)
  * lists that are not for blocking: child and liste_blanche (allow-lists),
    liste_bu (a French library list), reaffected, special, and the
    exam-specific examen_pix / tricheur_pix ("DO NOT USE")

Which categories are *enforced* is not decided here: it is read from
/etc/cache-proxy/filter-categories.conf (see config.block_categories()), which
the web UI writes.
"""
import re

from cache_proxy import config

# (name, group, description). `tarball=False` marks a category UT1's own
# tarball can't be used for (its malware tarball holds the phishing list), so
# it comes from another configured source instead.
_UT1 = [
    # Security
    ("malware", "Security", "Sites that deliver malware", False),
    ("phishing", "Security", "Phishing sites", True),
    ("cryptojacking", "Security", "Sites that mine cryptocurrency by hijacking visitors' browsers", True),
    ("stalkerware", "Security", "Sites that sell spying software", True),
    ("ddos", "Security", "DDoS and stresser sites", True),
    ("hacking", "Security", "Hacking sites", True),
    ("dangerous_material", "Security", "Sites describing how to make bombs and other dangerous material", True),
    ("dialer", "Security", "Dialer sites", True),
    ("warez", "Security", "Warez (pirated software) sites", True),
    # Adult
    ("adult", "Adult", "Adult sites, from erotic to hard pornography", True),
    ("mixed_adult", "Adult", "Sites with unstructured adult sections", True),
    ("lingerie", "Adult", "Lingerie sites", True),
    ("dating", "Adult", "Dating and matching sites", True),
    ("sexual_education", "Adult", "Sexual education (can be mis-detected as adult; block only if you need to)", True),
    # Gambling and games
    ("gambling", "Gambling and games", "Gambling, casinos and betting", True),
    ("arjel", "Gambling and games", "ARJEL, the French gambling certification authority", True),
    ("games", "Gambling and games", "Online and flash games", True),
    ("educational_games", "Gambling and games", "Educational online games", True),
    # Social and communication
    ("social_networks", "Social and communication", "Social networks", True),
    ("chat", "Social and communication", "Chat sites", True),
    ("forums", "Social and communication", "Forums", True),
    ("webmail", "Social and communication", "Webmail (Hotmail-style) sites", True),
    ("blog", "Social and communication", "Blog sites", True),
    # Media and content
    ("audio-video", "Media and content", "Audio and video sites", True),
    ("radio", "Media and content", "Internet radio", True),
    ("press", "Media and content", "Press and news sites", True),
    ("celebrity", "Media and content", "Celebrities and the magazines about them", True),
    ("manga", "Media and content", "Manga and cartoons", True),
    ("fakenews", "Media and content", "Fake-news sites", True),
    ("astrology", "Media and content", "Astrology", True),
    ("sect", "Media and content", "Sects", True),
    ("associations_religieuses", "Media and content", "Religious associations", True),
    ("agressif", "Media and content", "Aggressive sites", True),
    ("drogue", "Media and content", "Sites relating to drugs", True),
    ("sports", "Media and content", "Sports", True),
    ("cooking", "Media and content", "Cooking", True),
    ("translation", "Media and content", "Translation sites", True),
    ("ai", "Media and content", "Sites that provide artificial intelligence", True),
    # Money and shopping
    ("shopping", "Money and shopping", "Shopping and selling sites", True),
    ("bank", "Money and shopping", "Online banks", True),
    ("financial", "Money and shopping", "Financial information sites", True),
    ("jobsearch", "Money and shopping", "Job-search sites", True),
    ("bitcoin", "Money and shopping", "Bitcoin mining sites", True),
    ("marketingware", "Money and shopping", "Very special marketing sites", True),
    ("publicite", "Money and shopping", "Advertising", True),
    ("tricheur", "Money and shopping", "Sites that explain how to cheat on exams", True),
    # Circumvention, network and downloads
    ("vpn", "Circumvention and network", "VPN sites", True),
    ("redirector", "Circumvention and network", "Redirector sites used to get round filtering", True),
    ("strict_redirector", "Circumvention and network", "As redirector, plus search-engine cache/image robots", True),
    ("strong_redirector", "Circumvention and network", "As strict_redirector, but only blocks some search terms", True),
    ("doh", "Circumvention and network", "DNS-over-HTTPS providers (can be used to bypass DNS filtering)", True),
    ("dynamic-dns", "Circumvention and network", "Dynamic DNS providers", True),
    ("shortener", "Circumvention and network", "URL shorteners", True),
    ("remote-control", "Circumvention and network", "Sites that allow remote control of a desktop", True),
    ("residential-proxies", "Circumvention and network", "Residential proxy providers", True),
    ("filehosting", "Circumvention and network", "File-hosting sites (pictures, video, ...)", True),
    ("webhosting", "Circumvention and network", "Web-hosting providers", True),
    ("download", "Circumvention and network", "Sites that offer software downloads", True),
    ("update", "Circumvention and network", "Software and OS update sites (blocking these can stop updates)", True),
    ("cleaning", "Circumvention and network", "Sites to disinfect, update and protect computers", True),
    ("mobile-phone", "Circumvention and network", "Mobile-phone sites (ringtones etc.)", True),
]

CATALOGUE = [{"name": n, "group": g, "description": d, "tarball": t} for n, g, d, t in _UT1]
GROUP_ORDER = ["Security", "Adult", "Gambling and games", "Social and communication",
               "Media and content", "Money and shopping", "Circumvention and network", "Other"]
UT1_TARBALL_CATEGORIES = [c["name"] for c in CATALOGUE if c["tarball"]]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def source_categories(source: dict) -> list:
    """Categories a configured source can supply. A ut1 source with no
    `categories` key offers every UT1 tarball category."""
    cats = source.get("categories") or ([source["category"]] if source.get("category") else None)
    if cats is None and source.get("type") == "ut1":
        cats = UT1_TARBALL_CATEGORIES
    return [c.lower() for c in (cats or [])]


def available() -> list:
    """Every category that can be enforced: the catalogue plus anything else
    a configured source offers. Each entry also names the sources that supply
    it; a category with no source can't be downloaded and is left out."""
    supplied: dict = {}
    for src in config.FILTER_SOURCES:
        for cat in source_categories(src):
            supplied.setdefault(cat, []).append(src["name"])
    known = {c["name"]: c for c in CATALOGUE}
    out = []
    for name, sources in supplied.items():
        meta = known.get(name) or {"name": name, "group": "Other", "description": "Provided by a source you added"}
        out.append({**meta, "sources": sources})
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    out.sort(key=lambda c: (order.get(c["group"], len(order)), c["name"]))
    return out


def group(categories: list) -> list:
    """[(group name, [category, ...]), ...] in display order."""
    grouped: dict = {}
    for c in categories:
        grouped.setdefault(c["group"], []).append(c)
    return [(g, grouped[g]) for g in GROUP_ORDER if g in grouped] + \
           [(g, v) for g, v in grouped.items() if g not in GROUP_ORDER]


def render_categories_file(names: list) -> str:
    return (
        "# Content-filter categories that are enforced, one per line. Written by the\n"
        "# web UI Categories page; editing by hand is fine too. Overrides\n"
        "# [filtering] block_categories in config.toml. Changes apply within about 10\n"
        "# seconds; lists for newly added categories are downloaded on save.\n"
        + "".join(f"{n}\n" for n in names)
    )


def save_selected(names: list) -> None:
    """Write the selection in place (the web UI user can write this one file
    but not create files in /etc/cache-proxy). One small write; the proxy
    ignores a partial read because it re-reads on the next tick."""
    with open(config.CATEGORIES_FILE, "w", encoding="utf-8") as f:
        f.write(render_categories_file(names))
