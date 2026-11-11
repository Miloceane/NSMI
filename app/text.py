import re
import unicodedata


def strip_accents(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def normalize(text):
    """Lowercase, no accents, only a-z0-9. Used for exact name comparison."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text))


def words(text):
    """Lowercase, no accents, words separated by single spaces."""
    return " ".join(re.sub(r"[^a-z0-9]", " ", strip_accents(text)).split())


def mentions(haystack_words, name):
    """True if `name` appears in the text as whole words ("Ede" must not
    match inside "verleden")."""
    needle = words(name)
    return bool(needle) and f" {needle} " in f" {haystack_words} "
