"""Generate the frozen raw negative fixtures for the successor wire protocol (S4.2).

Each fixture is a single document representing one malformed or edge-sized side of the
wire: either the request bytes a future Go decoder must reject, or the response bytes a
future Python decoder must reject. The binary fixtures (``invalid-utf8.bin``,
``lone-surrogate.bin``) are written with explicit byte escapes because they are not valid
UTF-8 text and cannot round-trip through ``json.dumps``.

Run with ``python scripts/generate_protocol_negatives.py`` to regenerate all fourteen negative
fixtures in ``tests/fixtures/github_ci_successor_checkpoint/protocol/negative/`` and the two
at-limit boundary fixtures in ``.../protocol/boundary/`` (``source-count-at-limit.json`` and
``max-length-four-byte-source.json``). The boundary fixtures are legitimate, maximally sized
requests that pin the S4.2 cap compositions; this script asserts each composition at
generation time so a future change to the caps or the encoding rule is caught here rather
than silently drifting. Unlike the negatives, boundary fixtures are valid at-cap inputs for
the gate-9 harness and carry a trailing newline.
"""

import json
from pathlib import Path

CHECKPOINT = Path(__file__).parent.parent / "tests" / "fixtures" / "github_ci_successor_checkpoint"
NEGATIVE = CHECKPOINT / "protocol" / "negative"
BOUNDARY = CHECKPOINT / "protocol" / "boundary"

AGGREGATE_REQUEST_CAP_BYTES = 8_388_608
PER_SOURCE_CHARACTER_CAP = 1_048_576
MAX_SOURCES_PER_BATCH = 4_096
JSON_MAX_DEPTH = 64


def write_text(name: str, content: str) -> None:
    """Write a text-mode negative fixture verbatim, without a trailing-newline rewrite."""
    (NEGATIVE / name).write_text(content, encoding="utf-8")


def write_bytes(name: str, content: bytes) -> None:
    """Write a binary-mode negative fixture verbatim."""
    (NEGATIVE / name).write_bytes(content)


def generate_duplicate_keys() -> None:
    """Request: the ``id`` key repeats within one source object (Go decoder target)."""
    write_text(
        "duplicate-keys.json",
        '{"protocol_version":1,"sources":[{"id":0,"source":"true","id":0}]}',
    )


def generate_invalid_utf8() -> None:
    """Request: a lone 0xFF byte breaks strict UTF-8 inside the source string."""
    prefix = b'{"protocol_version":1,"sources":[{"id":0,"source":"X'
    suffix = b'X"}]}'
    write_bytes("invalid-utf8.bin", prefix + b"\xff" + suffix)


def generate_lone_surrogate() -> None:
    """Request: a UTF-8-shaped encoding of the lone surrogate U+D800 (ED A0 80).

    This covers the raw invalid-UTF-8 form (CESU-8 surrogate bytes) that breaks strict UTF-8
    decoding before any JSON parse. Its sibling ``escaped-lone-surrogate.json`` covers the
    decoder-level escaped form (a ``\\uD800`` escape inside valid ASCII JSON) that Go's
    encoding/json would silently replace with U+FFFD.
    """
    prefix = b'{"protocol_version":1,"sources":[{"id":0,"source":"X'
    suffix = b'X"}]}'
    write_bytes("lone-surrogate.bin", prefix + b"\xed\xa0\x80" + suffix)


def generate_escaped_lone_surrogate() -> None:
    """Request: a byte-exact ASCII JSON ``\\uD800`` escape decoding to a lone surrogate.

    Unlike ``lone-surrogate.bin`` (raw invalid UTF-8), this fixture is valid ASCII JSON whose
    single source value carries the six-character escape sequence backslash-u-D-8-0-0. The bytes
    are written literally so no JSON library normalizes the escape. A strict decoder must reject
    it, because Go's encoding/json silently replaces the decoded lone surrogate with U+FFFD.
    """
    write_text(
        "escaped-lone-surrogate.json",
        '{"protocol_version":1,"sources":[{"id":0,"source":"X\\uD800X"}]}',
    )


def generate_trailing_document() -> None:
    """Request: a valid document immediately followed by a second JSON value."""
    first = json.dumps(
        {"protocol_version": 1, "sources": [{"id": 0, "source": "true"}]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    write_text("trailing-document.json", first + '{"unexpected":true}')


def generate_wrong_type_bool_as_int() -> None:
    """Response: a result ``id`` is JSON ``true`` instead of an integer."""
    write_text(
        "wrong-type-bool-as-int.json",
        json.dumps(
            {
                "protocol_version": 1,
                "helper_version": "0" * 64,
                "parser_version": "mvdan.cc/sh/v3@v3.13.1",
                "results": [{"id": True, "events": [], "work_units": 1}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def generate_non_contiguous_ids() -> None:
    """Request: source ids 0 and 2, skipping 1."""
    write_text(
        "non-contiguous-ids.json",
        json.dumps(
            {
                "protocol_version": 1,
                "sources": [{"id": 0, "source": "true"}, {"id": 2, "source": "false"}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def generate_empty_batch() -> None:
    """Request: an empty ``sources`` array."""
    write_text(
        "empty-batch.json",
        json.dumps(
            {"protocol_version": 1, "sources": []}, ensure_ascii=False, separators=(",", ":")
        ),
    )


def generate_nan_number() -> None:
    """Response: ``work_units`` is the non-finite token ``NaN``."""
    write_text(
        "nan-number.json",
        '{"protocol_version":1,"helper_version":"' + "0" * 64 + '",'
        '"parser_version":"mvdan.cc/sh/v3@v3.13.1",'
        '"results":[{"id":0,"events":[],"work_units":NaN}]}',
    )


def generate_unknown_field() -> None:
    """Request: an undeclared top-level field."""
    write_text(
        "unknown-field.json",
        json.dumps(
            {
                "protocol_version": 1,
                "sources": [{"id": 0, "source": "true"}],
                "unexpected": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def generate_out_of_order_results() -> None:
    """Response: results carry ids 1 then 0 instead of ascending order."""
    write_text(
        "out-of-order-results.json",
        json.dumps(
            {
                "protocol_version": 1,
                "helper_version": "0" * 64,
                "parser_version": "mvdan.cc/sh/v3@v3.13.1",
                "results": [
                    {"id": 1, "events": [], "work_units": 1},
                    {"id": 0, "events": [], "work_units": 1},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def generate_span_out_of_range() -> None:
    """Response: an event's ``start_byte`` exceeds its ``end_byte`` (S3.3 range rule)."""
    write_text(
        "span-out-of-range.json",
        json.dumps(
            {
                "protocol_version": 1,
                "helper_version": "0" * 64,
                "parser_version": "mvdan.cc/sh/v3@v3.13.1",
                "results": [
                    {
                        "id": 0,
                        "events": [
                            {
                                "kind": "refusal",
                                "code": "syntax-error",
                                "start_byte": 10,
                                "end_byte": 5,
                            }
                        ],
                        "work_units": 1,
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def generate_max_length_four_byte_source() -> None:
    """Boundary: one source at the 1,048,576-character / 4,194,304-byte per-source cap.

    Pins the S4.2 cap composition: the inherited Python character cap (four-byte-worst-case)
    composes with the aggregate request byte cap once the canonical encoder rules
    (``ensure_ascii=False``, compact separators) are applied. Unlike the negatives, this is a
    legitimate at-cap input for the gate-9 harness, so it is written with a trailing newline
    into the sibling ``boundary/`` directory.
    """
    source = "\U0001f600" * PER_SOURCE_CHARACTER_CAP
    assert len(source) == PER_SOURCE_CHARACTER_CAP
    assert len(source.encode("utf-8")) == 4 * PER_SOURCE_CHARACTER_CAP
    request = {"protocol_version": 1, "sources": [{"id": 0, "source": source}]}
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    encoded_bytes = len(encoded.encode("utf-8"))
    assert encoded_bytes < AGGREGATE_REQUEST_CAP_BYTES, (
        f"max-length-four-byte-source request is {encoded_bytes} bytes, "
        f"at or over the aggregate cap of {AGGREGATE_REQUEST_CAP_BYTES}"
    )
    BOUNDARY.mkdir(parents=True, exist_ok=True)
    (BOUNDARY / "max-length-four-byte-source.json").write_text(encoded + "\n", encoding="utf-8")


def _sources_request(count: int) -> str:
    """Encode a canonical request carrying ``count`` trivial ``true`` sources, ids 0..count-1."""
    request = {
        "protocol_version": 1,
        "sources": [{"id": index, "source": "true"} for index in range(count)],
    }
    return json.dumps(request, ensure_ascii=False, separators=(",", ":"))


def generate_source_count_over_limit() -> None:
    """Request: a valid document with one source past the S4.4 per-batch source cap (4,097)."""
    write_text("source-count-over-limit.json", _sources_request(MAX_SOURCES_PER_BATCH + 1))


def generate_json_depth_over_limit() -> None:
    """Request bytes: nesting one array past the S4.4 JSON depth cap (65 arrays deep)."""
    depth = JSON_MAX_DEPTH + 1
    write_text("json-depth-over-limit.json", "[" * depth + "]" * depth)


def generate_source_count_at_limit() -> None:
    """Boundary: a valid document at exactly the S4.4 per-batch source cap (4,096 sources).

    Unlike the negatives, this is a legitimate at-cap input for the gate-9 harness, so it is
    written with a trailing newline into the sibling ``boundary/`` directory.
    """
    BOUNDARY.mkdir(parents=True, exist_ok=True)
    encoded = _sources_request(MAX_SOURCES_PER_BATCH)
    (BOUNDARY / "source-count-at-limit.json").write_text(encoded + "\n", encoding="utf-8")


def main() -> None:
    """Regenerate every negative fixture and both at-limit boundary fixtures."""
    NEGATIVE.mkdir(parents=True, exist_ok=True)
    generate_duplicate_keys()
    generate_invalid_utf8()
    generate_lone_surrogate()
    generate_escaped_lone_surrogate()
    generate_trailing_document()
    generate_wrong_type_bool_as_int()
    generate_non_contiguous_ids()
    generate_empty_batch()
    generate_nan_number()
    generate_unknown_field()
    generate_out_of_order_results()
    generate_span_out_of_range()
    generate_source_count_over_limit()
    generate_json_depth_over_limit()
    generate_max_length_four_byte_source()
    generate_source_count_at_limit()


if __name__ == "__main__":
    main()
