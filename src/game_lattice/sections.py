"""Heading-TOC and anchored-section extraction.

Section-span semantics are adapted from gx-linear-skills' binding_slicer: a section
spans from its heading line through the line before the next heading of equal or higher
level, or to end of file.
"""

import re
from dataclasses import dataclass

from .hashing import normalize_newlines

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# CommonMark optional closing sequence of an ATX heading: a trailing run of '#' preceded by
# whitespace is not part of the heading content, so GitHub discards it before slugging. We
# strip it so '## Save format ##' slugs to 'save-format', not 'save-format-'.
_ATX_CLOSING_RE = re.compile(r"\s+#+\s*$")
_ANCHOR_RE = re.compile(r"\s*\{#([A-Za-z0-9][A-Za-z0-9_-]*)\}\s*")
_FENCE_RE = re.compile(r"^ {0,3}(?P<ticks>`{3,}|~{3,})(?P<info>.*)$")
# Verbatim port of github-slugger@2.0.0's strip character class (its regex.js), the set
# of characters it replaces with the empty string before turning spaces into hyphens; see
# https://github.com/Flet/github-slugger/blob/v2.0.0/regex.js. Byte-parity with GitHub's
# rendered heading anchors is the requirement this satisfies (spec section 3), not a
# hand-rolled approximation, so this class must not be hand-edited: regenerate it from the
# pinned github-slugger version's regex.js if the pin ever changes. The source regex has no
# `u` flag and spells astral characters as UTF-16 surrogate-pair alternations; this port
# translates every JS \uXXXX escape unchanged and every surrogate-pair range to the
# equivalent astral codepoint range as a Python \UXXXXXXXX escape, since Python's re
# operates on codepoints, not UTF-16 code units. Verified codepoint-for-codepoint (0 to
# 0x10FFFF) against the real github-slugger@2.0.0 regex; see task-1-fix-report.md.
_SLUG_STRIP_RE = re.compile(
    r"[\u0000-\u001F\u0021-\u002C\u002E\u002F\u003A-\u0040\u005B-\u005E\u0060\u007B-\u00A9\u00AB-\u00B4\u00B6-\u00B9\u00BB-\u00BF\u00D7\u00F7\u02C2-\u02C5\u02D2-\u02DF\u02E5-\u02EB\u02ED\u02EF-\u02FF\u0375\u0378\u0379\u037E\u0380-\u0385\u0387\u038B\u038D\u03A2\u03F6\u0482\u0530\u0557\u0558\u055A-\u055F\u0589-\u0590\u05BE\u05C0\u05C3\u05C6\u05C8-\u05CF\u05EB-\u05EE\u05F3-\u060F\u061B-\u061F\u066A-\u066D\u06D4\u06DD\u06DE\u06E9\u06FD\u06FE\u0700-\u070F\u074B\u074C\u07B2-\u07BF\u07F6-\u07F9\u07FB\u07FC\u07FE\u07FF\u082E-\u083F\u085C-\u085F\u086B-\u089F\u08B5\u08C8-\u08D2\u08E2\u0964\u0965\u0970\u0984\u098D\u098E\u0991\u0992\u09A9\u09B1\u09B3-\u09B5\u09BA\u09BB\u09C5\u09C6\u09C9\u09CA\u09CF-\u09D6\u09D8-\u09DB\u09DE\u09E4\u09E5\u09F2-\u09FB\u09FD\u09FF\u0A00\u0A04\u0A0B-\u0A0E\u0A11\u0A12\u0A29\u0A31\u0A34\u0A37\u0A3A\u0A3B\u0A3D\u0A43-\u0A46\u0A49\u0A4A\u0A4E-\u0A50\u0A52-\u0A58\u0A5D\u0A5F-\u0A65\u0A76-\u0A80\u0A84\u0A8E\u0A92\u0AA9\u0AB1\u0AB4\u0ABA\u0ABB\u0AC6\u0ACA\u0ACE\u0ACF\u0AD1-\u0ADF\u0AE4\u0AE5\u0AF0-\u0AF8\u0B00\u0B04\u0B0D\u0B0E\u0B11\u0B12\u0B29\u0B31\u0B34\u0B3A\u0B3B\u0B45\u0B46\u0B49\u0B4A\u0B4E-\u0B54\u0B58-\u0B5B\u0B5E\u0B64\u0B65\u0B70\u0B72-\u0B81\u0B84\u0B8B-\u0B8D\u0B91\u0B96-\u0B98\u0B9B\u0B9D\u0BA0-\u0BA2\u0BA5-\u0BA7\u0BAB-\u0BAD\u0BBA-\u0BBD\u0BC3-\u0BC5\u0BC9\u0BCE\u0BCF\u0BD1-\u0BD6\u0BD8-\u0BE5\u0BF0-\u0BFF\u0C0D\u0C11\u0C29\u0C3A-\u0C3C\u0C45\u0C49\u0C4E-\u0C54\u0C57\u0C5B-\u0C5F\u0C64\u0C65\u0C70-\u0C7F\u0C84\u0C8D\u0C91\u0CA9\u0CB4\u0CBA\u0CBB\u0CC5\u0CC9\u0CCE-\u0CD4\u0CD7-\u0CDD\u0CDF\u0CE4\u0CE5\u0CF0\u0CF3-\u0CFF\u0D0D\u0D11\u0D45\u0D49\u0D4F-\u0D53\u0D58-\u0D5E\u0D64\u0D65\u0D70-\u0D79\u0D80\u0D84\u0D97-\u0D99\u0DB2\u0DBC\u0DBE\u0DBF\u0DC7-\u0DC9\u0DCB-\u0DCE\u0DD5\u0DD7\u0DE0-\u0DE5\u0DF0\u0DF1\u0DF4-\u0E00\u0E3B-\u0E3F\u0E4F\u0E5A-\u0E80\u0E83\u0E85\u0E8B\u0EA4\u0EA6\u0EBE\u0EBF\u0EC5\u0EC7\u0ECE\u0ECF\u0EDA\u0EDB\u0EE0-\u0EFF\u0F01-\u0F17\u0F1A-\u0F1F\u0F2A-\u0F34\u0F36\u0F38\u0F3A-\u0F3D\u0F48\u0F6D-\u0F70\u0F85\u0F98\u0FBD-\u0FC5\u0FC7-\u0FFF\u104A-\u104F\u109E\u109F\u10C6\u10C8-\u10CC\u10CE\u10CF\u10FB\u1249\u124E\u124F\u1257\u1259\u125E\u125F\u1289\u128E\u128F\u12B1\u12B6\u12B7\u12BF\u12C1\u12C6\u12C7\u12D7\u1311\u1316\u1317\u135B\u135C\u1360-\u137F\u1390-\u139F\u13F6\u13F7\u13FE-\u1400\u166D\u166E\u1680\u169B-\u169F\u16EB-\u16ED\u16F9-\u16FF\u170D\u1715-\u171F\u1735-\u173F\u1754-\u175F\u176D\u1771\u1774-\u177F\u17D4-\u17D6\u17D8-\u17DB\u17DE\u17DF\u17EA-\u180A\u180E\u180F\u181A-\u181F\u1879-\u187F\u18AB-\u18AF\u18F6-\u18FF\u191F\u192C-\u192F\u193C-\u1945\u196E\u196F\u1975-\u197F\u19AC-\u19AF\u19CA-\u19CF\u19DA-\u19FF\u1A1C-\u1A1F\u1A5F\u1A7D\u1A7E\u1A8A-\u1A8F\u1A9A-\u1AA6\u1AA8-\u1AAF\u1AC1-\u1AFF\u1B4C-\u1B4F\u1B5A-\u1B6A\u1B74-\u1B7F\u1BF4-\u1BFF\u1C38-\u1C3F\u1C4A-\u1C4C\u1C7E\u1C7F\u1C89-\u1C8F\u1CBB\u1CBC\u1CC0-\u1CCF\u1CD3\u1CFB-\u1CFF\u1DFA\u1F16\u1F17\u1F1E\u1F1F\u1F46\u1F47\u1F4E\u1F4F\u1F58\u1F5A\u1F5C\u1F5E\u1F7E\u1F7F\u1FB5\u1FBD\u1FBF-\u1FC1\u1FC5\u1FCD-\u1FCF\u1FD4\u1FD5\u1FDC-\u1FDF\u1FED-\u1FF1\u1FF5\u1FFD-\u203E\u2041-\u2053\u2055-\u2070\u2072-\u207E\u2080-\u208F\u209D-\u20CF\u20F1-\u2101\u2103-\u2106\u2108\u2109\u2114\u2116-\u2118\u211E-\u2123\u2125\u2127\u2129\u212E\u213A\u213B\u2140-\u2144\u214A-\u214D\u214F-\u215F\u2189-\u24B5\u24EA-\u2BFF\u2C2F\u2C5F\u2CE5-\u2CEA\u2CF4-\u2CFF\u2D26\u2D28-\u2D2C\u2D2E\u2D2F\u2D68-\u2D6E\u2D70-\u2D7E\u2D97-\u2D9F\u2DA7\u2DAF\u2DB7\u2DBF\u2DC7\u2DCF\u2DD7\u2DDF\u2E00-\u2E2E\u2E30-\u3004\u3008-\u3020\u3030\u3036\u3037\u303D-\u3040\u3097\u3098\u309B\u309C\u30A0\u30FB\u3100-\u3104\u3130\u318F-\u319F\u31C0-\u31EF\u3200-\u33FF\u4DC0-\u4DFF\u9FFD-\u9FFF\uA48D-\uA4CF\uA4FE\uA4FF\uA60D-\uA60F\uA62C-\uA63F\uA673\uA67E\uA6F2-\uA716\uA720\uA721\uA789\uA78A\uA7C0\uA7C1\uA7CB-\uA7F4\uA828-\uA82B\uA82D-\uA83F\uA874-\uA87F\uA8C6-\uA8CF\uA8DA-\uA8DF\uA8F8-\uA8FA\uA8FC\uA92E\uA92F\uA954-\uA95F\uA97D-\uA97F\uA9C1-\uA9CE\uA9DA-\uA9DF\uA9FF\uAA37-\uAA3F\uAA4E\uAA4F\uAA5A-\uAA5F\uAA77-\uAA79\uAAC3-\uAADA\uAADE\uAADF\uAAF0\uAAF1\uAAF7-\uAB00\uAB07\uAB08\uAB0F\uAB10\uAB17-\uAB1F\uAB27\uAB2F\uAB5B\uAB6A-\uAB6F\uABEB\uABEE\uABEF\uABFA-\uABFF\uD7A4-\uD7AF\uD7C7-\uD7CA\uD7FC-\uD7FF\uE000-\uF8FF\uFA6E\uFA6F\uFADA-\uFAFF\uFB07-\uFB12\uFB18-\uFB1C\uFB29\uFB37\uFB3D\uFB3F\uFB42\uFB45\uFBB2-\uFBD2\uFD3E-\uFD4F\uFD90\uFD91\uFDC8-\uFDEF\uFDFC-\uFDFF\uFE10-\uFE1F\uFE30-\uFE32\uFE35-\uFE4C\uFE50-\uFE6F\uFE75\uFEFD-\uFF0F\uFF1A-\uFF20\uFF3B-\uFF3E\uFF40\uFF5B-\uFF65\uFFBF-\uFFC1\uFFC8\uFFC9\uFFD0\uFFD1\uFFD8\uFFD9\uFFDD-\uFFFF\U0001000C\U00010027\U0001003B\U0001003E\U0001004E\U0001004F\U0001005E-\U0001007F\U000100FB-\U0001013F\U00010175-\U000101FC\U000101FE-\U0001027F\U0001029D-\U0001029F\U000102D1-\U000102DF\U000102E1-\U000102FF\U00010320-\U0001032C\U0001034B-\U0001034F\U0001037B-\U0001037F\U0001039E\U0001039F\U000103C4-\U000103C7\U000103D0\U000103D6-\U000103FF\U0001049E\U0001049F\U000104AA-\U000104AF\U000104D4-\U000104D7\U000104FC-\U000104FF\U00010528-\U0001052F\U00010564-\U000105FF\U00010737-\U0001073F\U00010756-\U0001075F\U00010768-\U000107FF\U00010806\U00010807\U00010809\U00010836\U00010839-\U0001083B\U0001083D\U0001083E\U00010856-\U0001085F\U00010877-\U0001087F\U0001089F-\U000108DF\U000108F3\U000108F6-\U000108FF\U00010916-\U0001091F\U0001093A-\U0001097F\U000109B8-\U000109BD\U000109C0-\U000109FF\U00010A04\U00010A07-\U00010A0B\U00010A14\U00010A18\U00010A36\U00010A37\U00010A3B-\U00010A3E\U00010A40-\U00010A5F\U00010A7D-\U00010A7F\U00010A9D-\U00010ABF\U00010AC8\U00010AE7-\U00010AFF\U00010B36-\U00010B3F\U00010B56-\U00010B5F\U00010B73-\U00010B7F\U00010B92-\U00010BFF\U00010C49-\U00010C7F\U00010CB3-\U00010CBF\U00010CF3-\U00010CFF\U00010D28-\U00010D2F\U00010D3A-\U00010E7F\U00010EAA\U00010EAD-\U00010EAF\U00010EB2-\U00010EFF\U00010F1D-\U00010F26\U00010F28-\U00010F2F\U00010F51-\U00010FAF\U00010FC5-\U00010FDF\U00010FF7-\U00010FFF\U00011047-\U00011065\U00011070-\U0001107E\U000110BB-\U000110CF\U000110E9-\U000110EF\U000110FA-\U000110FF\U00011135\U00011140-\U00011143\U00011148-\U0001114F\U00011174\U00011175\U00011177-\U0001117F\U000111C5-\U000111C8\U000111CD\U000111DB\U000111DD-\U000111FF\U00011212\U00011238-\U0001123D\U0001123F-\U0001127F\U00011287\U00011289\U0001128E\U0001129E\U000112A9-\U000112AF\U000112EB-\U000112EF\U000112FA-\U000112FF\U00011304\U0001130D\U0001130E\U00011311\U00011312\U00011329\U00011331\U00011334\U0001133A\U00011345\U00011346\U00011349\U0001134A\U0001134E\U0001134F\U00011351-\U00011356\U00011358-\U0001135C\U00011364\U00011365\U0001136D-\U0001136F\U00011375-\U000113FF\U0001144B-\U0001144F\U0001145A-\U0001145D\U00011462-\U0001147F\U000114C6\U000114C8-\U000114CF\U000114DA-\U0001157F\U000115B6\U000115B7\U000115C1-\U000115D7\U000115DE-\U000115FF\U00011641-\U00011643\U00011645-\U0001164F\U0001165A-\U0001167F\U000116B9-\U000116BF\U000116CA-\U000116FF\U0001171B\U0001171C\U0001172C-\U0001172F\U0001173A-\U000117FF\U0001183B-\U0001189F\U000118EA-\U000118FE\U00011907\U00011908\U0001190A\U0001190B\U00011914\U00011917\U00011936\U00011939\U0001193A\U00011944-\U0001194F\U0001195A-\U0001199F\U000119A8\U000119A9\U000119D8\U000119D9\U000119E2\U000119E5-\U000119FF\U00011A3F-\U00011A46\U00011A48-\U00011A4F\U00011A9A-\U00011A9C\U00011A9E-\U00011ABF\U00011AF9-\U00011BFF\U00011C09\U00011C37\U00011C41-\U00011C4F\U00011C5A-\U00011C71\U00011C90\U00011C91\U00011CA8\U00011CB7-\U00011CFF\U00011D07\U00011D0A\U00011D37-\U00011D39\U00011D3B\U00011D3E\U00011D48-\U00011D4F\U00011D5A-\U00011D5F\U00011D66\U00011D69\U00011D8F\U00011D92\U00011D99-\U00011D9F\U00011DAA-\U00011EDF\U00011EF7-\U00011FAF\U00011FB1-\U00011FFF\U0001239A-\U000123FF\U0001246F-\U0001247F\U00012544-\U00012FFF\U0001342F-\U000143FF\U00014647-\U000167FF\U00016A39-\U00016A3F\U00016A5F\U00016A6A-\U00016ACF\U00016AEE\U00016AEF\U00016AF5-\U00016AFF\U00016B37-\U00016B3F\U00016B44-\U00016B4F\U00016B5A-\U00016B62\U00016B78-\U00016B7C\U00016B90-\U00016E3F\U00016E80-\U00016EFF\U00016F4B-\U00016F4E\U00016F88-\U00016F8E\U00016FA0-\U00016FDF\U00016FE2\U00016FE5-\U00016FEF\U00016FF2-\U00016FFF\U000187F8-\U000187FF\U00018CD6-\U00018CFF\U00018D09-\U0001AFFF\U0001B11F-\U0001B14F\U0001B153-\U0001B163\U0001B168-\U0001B16F\U0001B2FC-\U0001BBFF\U0001BC6B-\U0001BC6F\U0001BC7D-\U0001BC7F\U0001BC89-\U0001BC8F\U0001BC9A-\U0001BC9C\U0001BC9F-\U0001D164\U0001D16A-\U0001D16C\U0001D173-\U0001D17A\U0001D183\U0001D184\U0001D18C-\U0001D1A9\U0001D1AE-\U0001D241\U0001D245-\U0001D3FF\U0001D455\U0001D49D\U0001D4A0\U0001D4A1\U0001D4A3\U0001D4A4\U0001D4A7\U0001D4A8\U0001D4AD\U0001D4BA\U0001D4BC\U0001D4C4\U0001D506\U0001D50B\U0001D50C\U0001D515\U0001D51D\U0001D53A\U0001D53F\U0001D545\U0001D547-\U0001D549\U0001D551\U0001D6A6\U0001D6A7\U0001D6C1\U0001D6DB\U0001D6FB\U0001D715\U0001D735\U0001D74F\U0001D76F\U0001D789\U0001D7A9\U0001D7C3\U0001D7CC\U0001D7CD\U0001D800-\U0001D9FF\U0001DA37-\U0001DA3A\U0001DA6D-\U0001DA74\U0001DA76-\U0001DA83\U0001DA85-\U0001DA9A\U0001DAA0\U0001DAB0-\U0001DFFF\U0001E007\U0001E019\U0001E01A\U0001E022\U0001E025\U0001E02B-\U0001E0FF\U0001E12D-\U0001E12F\U0001E13E\U0001E13F\U0001E14A-\U0001E14D\U0001E14F-\U0001E2BF\U0001E2FA-\U0001E7FF\U0001E8C5-\U0001E8CF\U0001E8D7-\U0001E8FF\U0001E94C-\U0001E94F\U0001E95A-\U0001EDFF\U0001EE04\U0001EE20\U0001EE23\U0001EE25\U0001EE26\U0001EE28\U0001EE33\U0001EE38\U0001EE3A\U0001EE3C-\U0001EE41\U0001EE43-\U0001EE46\U0001EE48\U0001EE4A\U0001EE4C\U0001EE50\U0001EE53\U0001EE55\U0001EE56\U0001EE58\U0001EE5A\U0001EE5C\U0001EE5E\U0001EE60\U0001EE63\U0001EE65\U0001EE66\U0001EE6B\U0001EE73\U0001EE78\U0001EE7D\U0001EE7F\U0001EE8A\U0001EE9C-\U0001EEA0\U0001EEA4\U0001EEAA\U0001EEBC-\U0001F12F\U0001F14A-\U0001F14F\U0001F16A-\U0001F16F\U0001F18A-\U0001FBEF\U0001FBFA-\U0001FFFF\U0002A6DE-\U0002A6FF\U0002B735-\U0002B73F\U0002B81E\U0002B81F\U0002CEA2-\U0002CEAF\U0002EBE1-\U0002F7FF\U0002FA1E-\U0002FFFF\U0003134B-\U000E00FF\U000E01F0-\U0010FFFF]"
)


@dataclass(frozen=True, slots=True)
class Heading:
    """One markdown heading. ``line`` is 1-indexed. ``text`` keeps the anchor marker but has
    any CommonMark ATX closing ``#`` sequence stripped, matching GitHub's rendered anchor."""

    level: int
    text: str
    anchor: str | None
    line: int


def split_body_lines(body: str) -> list[str]:
    """Split ``body`` into lines on ``\\n`` only, matching the hashing model.

    Unlike ``str.splitlines``, this does not treat form feed, vertical tab, NEL, or the
    Unicode line/paragraph separators as line breaks, so an exotic separator inside
    content cannot spawn a phantom heading or anchor. Line endings are normalized first
    and a single trailing blank (from a final newline) is dropped, so the result matches
    ``str.splitlines`` for ordinary text.

    Args:
        body: Markdown document text.

    Returns:
        The lines of ``body``.
    """
    lines = normalize_newlines(body).split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def build_toc(body: str) -> list[Heading]:
    """Return all ATX headings in ``body`` in document order.

    Headings inside fenced code blocks (delimited by ``` or ~~~) are ignored, so a
    ``#``-prefixed comment or a ``{#id}`` token inside a code sample is not mistaken for
    a heading or anchor.

    Args:
        body: Markdown document text.

    Returns:
        A list of Heading, each with its level, text, optional ``{#anchor}`` id, and
        1-indexed line number.
    """
    headings: list[Heading] = []
    open_fence: str | None = None
    for i, line in enumerate(split_body_lines(body), start=1):
        fence_match = _FENCE_RE.match(line)
        if open_fence is None:
            if fence_match is not None:
                open_fence = fence_match.group("ticks")
                continue
        else:
            # CommonMark closing-fence rule: a fence closes only on the same fence
            # character (backtick or tilde), a run at least as long as the opener, and
            # nothing after it. A shorter run or a trailing info string keeps the block
            # open, so those lines stay code content and never register as headings.
            is_closing_fence = (
                fence_match is not None
                and fence_match.group("ticks")[0] == open_fence[0]
                and len(fence_match.group("ticks")) >= len(open_fence)
                and not fence_match.group("info").strip()
            )
            if is_closing_fence:
                open_fence = None
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        raw_text = _ATX_CLOSING_RE.sub("", match.group(2))
        anchor_match = _ANCHOR_RE.search(raw_text)
        anchor = anchor_match.group(1) if anchor_match else None
        headings.append(Heading(level=level, text=raw_text, anchor=anchor, line=i))
    return headings


def section_span(headings: list[Heading], idx: int, total_lines: int) -> tuple[int, int]:
    """Return the inclusive 1-indexed line range for ``headings[idx]``.

    Args:
        headings: The document TOC from ``build_toc``.
        idx: Index into ``headings`` of the section of interest.
        total_lines: Total line count of the document.

    Returns:
        ``(start, end)`` from the heading line through the line before the next heading
        of equal or higher level, or to ``total_lines``.
    """
    head = headings[idx]
    end = total_lines
    for nxt in headings[idx + 1 :]:
        if nxt.level <= head.level:
            end = nxt.line - 1
            break
    return (head.line, end)


def section_text(body: str, span: tuple[int, int]) -> str:
    """Return the text of a section span with the heading's ``{#anchor}`` marker removed.

    Args:
        body: Markdown document text.
        span: Inclusive 1-indexed ``(start, end)`` line range.

    Returns:
        The joined lines of the span, with the anchor marker stripped from the first
        (heading) line.
    """
    lines = split_body_lines(body)
    start, end = span
    chunk = lines[start - 1 : end]
    if chunk:
        chunk[0] = _ANCHOR_RE.sub(" ", chunk[0]).rstrip()
    return "\n".join(chunk)


def github_slug(text: str) -> str:
    """Return the github-slugger slug of a heading's text (without de-duping).

    This is a verbatim port of github-slugger@2.0.0's ``slug()``, not an approximation:
    lowercase the text, strip every character in the ported class, then turn each space into
    a hyphen, in that order, matching github-slugger's ``index.js`` exactly. Runs are not
    collapsed, matching github-slugger: two spaces become two hyphens. De-duping across a
    document is handled by ``anchor_ids``.

    Args:
        text: One heading's text (the marker, if any, is part of the text and is slugged).

    Returns:
        The lowercase, stripped, hyphen-joined slug, byte-parity with github-slugger@2.0.0.
    """
    return _SLUG_STRIP_RE.sub("", text.lower()).replace(" ", "-")


class _Slugger:
    """Document-order slug de-duper mirroring github-slugger's occurrence counter.

    The first time a base slug appears it is emitted and reserved; each later appearance is
    suffixed ``-1``, ``-2``, and so on, and every emitted result is reserved so a later
    identical base cannot reuse it.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def slug(self, text: str) -> str:
        """Return the unique slug for ``text`` given every slug emitted so far."""
        base = github_slug(text)
        result = base
        while result in self._seen:
            self._seen[base] += 1
            result = f"{base}-{self._seen[base]}"
        self._seen[result] = 0
        return result


def anchor_ids(headings: list[Heading]) -> list[str]:
    """Return one addressable anchor id per heading, in document order.

    A heading with an explicit ``{#marker}`` is addressed by its marker; every other heading
    is addressed by its de-duped GitHub slug. Every heading (marker or not) reserves its
    GitHub slug in the shared counter, so the markerless headings around a marker heading are
    suffixed exactly as GitHub would suffix them.

    Args:
        headings: The document TOC from ``build_toc``, in document order.

    Returns:
        A list of anchor ids positionally aligned with ``headings``.
    """
    slugger = _Slugger()
    ids: list[str] = []
    for heading in headings:
        unique = slugger.slug(heading.text)
        ids.append(heading.anchor if heading.anchor is not None else unique)
    return ids
