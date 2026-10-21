"""Secrets are never kept: one place that knows what a password, a key, a code or a card number looks like.

* ``has_secret_value(text)``: the text carries an actual secret (an API key, a private key, a card number, or
  "the password is hunter2"). Notes refuse these, and they're blanked in the conversation log.
* ``mentions_secret(text)``: the text is *about* a secret at all ("the wifi password is on the fridge"). Memory never
  learns a fact like that on its own; a note J asks for is fine, as long as it doesn't hold the value.
* ``redact(text)``: the same text with every secret value replaced by ``[secret]``.
"""

from __future__ import annotations

import re

REDACTED = "[secret]"

# API keys and tokens: OpenAI/Anthropic, GitHub, Slack, AWS, Google, GitLab, Telegram bots, JWTs, Discord webhooks
TOKEN = re.compile(r"\b(?:sk-[\w-]{16,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{40,}|xox[abposr]-[\w-]{10,}"
                   r"|AKIA[0-9A-Z]{16}|AIza[\w-]{35}|glpat-[\w-]{20,}|GOCSPX-[\w-]{20,}|\d{6,}:[A-Za-z0-9_-]{30,}"
                   r"|eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,})"
                   r"|https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
SECRET_NOUN = (r"(?:password|passwd|pwd|passcode|passphrase|pin(?: code| number)?|(?:security|access|alarm|door|gate|"
               r"garage|lock|safe|entry|building|keypad|wi-?fi) code|wi-?fi key|"
               r"2fa code|one[- ]time code|verification code|backup code|recovery code|"
               r"seed phrase|api[ _-]?key|secret key|access token|token|cvv|cvc|ssn|social security number)")
# "<noun> is X" / "<noun>: X" / "<noun> = X", where X is a value, not "on the fridge" or "in my password manager"
VALUE = re.compile(rf"(?i)\b({SECRET_NOUN})(?:\s+(?:is|was|are|=|:)|\s*[:=])\s*(?!\[secret\])(?!(?:on|in|at|the|my|a|an|written|under|"
                   r"saved|stored|kept|same|taped|somewhere|behind|inside|changed|expired|wrong|right|correct|"
                   r"case[- ]sensitive)\b)([^\s,;]{3,})")
MENTION = re.compile(rf"(?i)\b{SECRET_NOUN}s?\b|\bpin\b|\bcredit card\b|\bsocial security\b|\bbank account\b"
                     r"|\baccount number\b|\brouting number\b|\biban\b|\btokens?\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _cards(text: str):
    for m in CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            yield m


def has_secret_value(text: str) -> bool:
    t = text or ""
    return bool(TOKEN.search(t) or PRIVATE_KEY.search(t) or VALUE.search(t) or any(True for _ in _cards(t)))


def mentions_secret(text: str) -> bool:
    return has_secret_value(text) or bool(MENTION.search(text or ""))


def redact(text: str) -> str:
    t = text or ""
    if not t:
        return t
    t = PRIVATE_KEY.sub(REDACTED, t)
    t = TOKEN.sub(REDACTED, t)
    for m in reversed(list(_cards(t))):
        t = t[: m.start()] + REDACTED + t[m.end():]
    return VALUE.sub(lambda m: m.group(0)[: m.start(2) - m.start(0)] + REDACTED, t)
