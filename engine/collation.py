"""Sort order of `String.prototype.localeCompare` (ICU, CLDR root collation) for file names.

`npx skills` hashes a skill folder after sorting its file paths with `a.localeCompare(b)`
(skills 1.7.6, computeSkillFolderHash), so a `computedHash` only matches when the paths are sorted
the same way. Python has no ICU in the standard library; this module carries the part of the root
collation that file names use, taken from Node 26 (ICU 78.3, Unicode 17, locale en-US) and checked
against it by tests/test_plugins.py when node is installed.

Covered: printable ASCII, Latin-1 Supplement and Latin Extended-A (U+00A0..U+017F), compared on three
levels like ICU does (letters first, then accents, then case; space and punctuation sort before
digits, digits before letters). Text is NFC-normalized first.

Not covered, and why it cannot be: the CLI uses the default locale of the machine it runs on. Locales
that tailor Latin (sv, da, tr, cs, ...) and scripts outside the table (CJK is pinyin order under zh,
code point order under en) sort differently from machine to machine, so for such names the CLI's own
hash is not stable. Characters outside the table sort after the table, by code point.
"""
import unicodedata

# The table's characters in collation order, and for each how it differs from the one before it:
# 0 a new letter (primary), 1 another accent of the same letter (secondary), 2 another case (tertiary).
_ORDER = (
    " \xa0_-,;:!\xa1?\xbf.\xb7\'\"\xab\xbb()[]{}\xa7\xb6@*/\\&#%`\xb4^\xaf\xa8\xb8\xb0\xa9\xae+\xb1"
    "\xf7\xd7<=>\xac|\xa6~\xa4\xa2$\xa3\xa501\xb9\xbd\xbc2\xb23\xb3\xbe456789aA\xaa\xe1\xc1\xe0\xc0"
    "\u0103\u0102\xe2\xc2\xe5\xc5\xe4\xc4\xe3\xc3\u0105\u0104\u0101\u0100bBcC\u0107\u0106\u0109\u0108"
    "\u010d\u010c\u010b\u010a\xe7\xc7dD\u010f\u010e\u0111\u0110\xf0\xd0eE\xe9\xc9\xe8\xc8\u0115\u0114"
    "\xea\xca\u011b\u011a\xeb\xcb\u0117\u0116\u0119\u0118\u0113\u0112fFgG\u011f\u011e\u011d\u011c"
    "\u0121\u0120\u0123\u0122hH\u0125\u0124\u0127\u0126iI\xed\xcd\xec\xcc\u012d\u012c\xee\xce\xef\xcf"
    "\u0129\u0128\u0130\u012f\u012e\u012b\u012a\u0131jJ\u0135\u0134kK\u0137\u0136lL\u013a\u0139\u013e"
    "\u013d\u013c\u013b\u0142\u0141\u0140\u013fmMnN\u0144\u0143\u0148\u0147\xf1\xd1\u0146\u0145\u014b"
    "\u014aoO\xba\xf3\xd3\xf2\xd2\u014f\u014e\xf4\xd4\xf6\xd6\u0151\u0150\xf5\xd5\xf8\xd8\u014d\u014c"
    "pPqQ\u0138rR\u0155\u0154\u0159\u0158\u0157\u0156sS\u015b\u015a\u015d\u015c\u0161\u0160\u015f"
    "\u015e\u017ftT\u0165\u0164\u0163\u0162\u0167\u0166uU\xfa\xda\xf9\xd9\u016d\u016c\xfb\xdb\u016f"
    "\u016e\xfc\xdc\u0171\u0170\u0169\u0168\u0173\u0172\u016b\u016avVwW\u0175\u0174xXyY\xfd\xdd\u0177"
    "\u0176\xff\u0178zZ\u017a\u0179\u017e\u017d\u017c\u017b\xfe\xde\u0149\xb5"
)
_MARKS = (
    "020000000000000000000000000000000000000000000000000000000002000202000000002212121212121212121202"
    "021212121212021212120212121212121212121202021212121202121202121212121212112120021202120212121212"
    "120202121212120202212121212121212121202020021212120212121212102121202021212121212121212121202021"
    "20202121212021212120200"
)

# One character that sorts as two letters. (letters, has its own secondary weight, upper case)
_EXPANSIONS = {
    "\xe6": ("ae", True, False), "\xc6": ("ae", True, True),
    "\u0153": ("oe", True, False), "\u0152": ("oe", True, True),
    "\xdf": ("ss", True, False),
    "\u0133": ("ij", False, False), "\u0132": ("ij", False, True),
}
_IGNORABLE = "\xad"
# In the table's range but sorting as a sequence with a character outside it; treated as uncovered.
_UNCOVERED = "\xb7\xbc\xbd\xbe\u0149\u013f\u0140"  # the middle dot contracts with l (Catalan l·l)
_LIGATURE = 1000  # the secondary weight of a ligature, above every accent


def _build() -> dict:
    table, p, s, t = {}, -1, 0, 0
    for ch, mark in zip(_ORDER, _MARKS):
        if mark == "0":
            p, s, t = p + 1, 0, 0
        elif mark == "1":
            s, t = s + 1, 0
        else:
            t += 1
        table[ch] = (p, s, t)
    for ch in _UNCOVERED:
        del table[ch]
    return table


_TABLE = _build()


def covered(text: str) -> bool:
    """True when every character of `text` sorts by the table (ICU root order, checked against Node)."""
    return all(ch in _TABLE or ch in _EXPANSIONS or ch in _IGNORABLE for ch in unicodedata.normalize("NFC", text))


def sort_key(text: str) -> tuple:
    """A key that sorts like `a.localeCompare(b)` under the root collation, for the covered characters."""
    prim, sec, ter = [], [], []
    for ch in unicodedata.normalize("NFC", text):
        if ch in _IGNORABLE:
            continue
        exp = _EXPANSIONS.get(ch)
        if exp:
            letters, ligature, upper = exp
            case = 1.5 if upper else 0.5  # between the plain lower and upper case letter pairs
            for i, letter in enumerate(letters):
                prim.append((0, _TABLE[letter][0]))
                sec.append(0)
                ter.append(case)
                if ligature and i == 0:
                    sec.append(_LIGATURE)
                    ter.append(case)
            continue
        entry = _TABLE.get(ch)
        if entry is None:
            prim.append((1, ord(ch)))
            sec.append(0)
            ter.append(0)
            continue
        p, s, t = entry
        prim.append((0, p))
        sec.append(0)
        ter.append(t)
        if s:  # the accent is an element of its own after the letter, as in ICU
            sec.append(s)
            ter.append(0)
    return prim, sec, ter
