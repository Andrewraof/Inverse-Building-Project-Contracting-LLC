import re

_PHONE_STRIP_RE = re.compile(r'[\s().\-]')
UAE_PREFIX = '+971'


def normalize_email(value):
    """Conservative email normalization for dedup matching.

    Trims outer whitespace and lowercases. Returns '' for anything that
    is not structurally a single mailbox address — an empty or invalid
    value is never usable as a match key. The original value is kept
    untouched on the lead; this only feeds the normalized match column.
    """
    if not value:
        return ''
    v = str(value).strip().lower()
    if not v or len(v) > 254 or v.count('@') != 1:
        return ''
    if any(ch.isspace() for ch in v):
        return ''
    local, domain = v.rsplit('@', 1)
    if not local or not domain or '.' not in domain:
        return ''
    if domain.startswith('.') or domain.endswith('.') or '..' in domain:
        return ''
    return v


def normalize_phone(value, uae_context=False):
    """Conservative phone normalization for dedup matching.

    Removes spaces, parentheses, dashes and dots; converts the
    international '00' prefix to '+'. UAE forms are canonicalized to
    '+971...': '00971...' and '971...' (12 digits) always, and a local
    '0...' number only when uae_context is certain (company country AE).
    Ambiguous numbers are returned as bare cleaned digits with no country
    guess — they can only ever match an identically cleaned value, never
    a different country. Returns '' when the value is unusable.
    """
    if not value:
        return ''
    v = _PHONE_STRIP_RE.sub('', str(value))
    if not v:
        return ''
    if v.startswith('00'):
        v = '+' + v[2:]
    if v.startswith('+'):
        digits = v[1:]
        if digits.isdigit() and 7 <= len(digits) <= 15:
            return '+' + digits
        return ''
    if not v.isdigit():
        return ''
    if v.startswith('971') and len(v) == 12:
        return '+' + v
    if uae_context and v.startswith('0') and 9 <= len(v) <= 10:
        return UAE_PREFIX + v[1:]
    return v
