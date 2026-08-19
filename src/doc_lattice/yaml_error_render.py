"""Spell a YAML load failure's own message for human-facing output.

Every boundary that loads user-authored YAML catches ``YAML_LOAD_ERRORS`` and reports a
``ProjectError`` naming the file, interpolating the caught exception's message for the detail.
That message is built by ``ruamel`` rather than by this codebase, and it echoes document content
back at the reader: a duplicate-key constructor error quotes the offending key and both of its
values, so a block spelling a double-quoted ``\\u001b`` twice puts raw ESC bytes on stderr through
a diagnostic no rule of this project ever inspected. A load failure aborts before validation runs,
so AD-35's refusal cannot reach it, and the message is not built here, so AD-34's construction-site
spelling had nowhere to be applied until this module gave it one.

This is the third application of the axis AD-36 states: what decides between refusing a string and
spelling it is not how untrusted the string is but what else it does. A load failure's message is
display-only. It participates in no identity and in no structured output, because a document that
fails to load fails uniformly before format selection, so it takes AD-34's spelling at the sink.
See AD-37 for the decision and for the readability cost it accepts.

The module exists so the four handlers cannot drift apart on the spelling, and it sits beside
``validation_render.py`` for the same reason that one does: ``yaml_boundary.py`` owns the load
mechanics and deliberately not the caller's error policy, and how a failure is reported is policy.
"""


def format_yaml_error_for_display(exc: Exception) -> str:
    """Spell a caught YAML load failure for a message a person reads.

    The spelling is exactly ``repr(str(exc))``, which is AD-34's spelling for a path and injective
    for AD-34's reason: ``str.__repr__`` escapes every C0 code point, DEL, and the C1 range, so no
    two distinct messages render alike and none of them can carry a byte the terminal acts on.
    Applying it to the whole message rather than to a part is what the third-party origin forces.
    ``ruamel`` has already joined its context, marks, problem, and note into one string by the time
    a handler sees it, and a newline it wrote is indistinguishable there from one decoded out of an
    echoed key or value, so a spelling that preserved the message's own line breaks would let a
    document forge a diagnostic line, which is the rule AD-35 records for a frontmatter value.

    Callers keep their own domain header and interpolate the result as the detail, so what a file
    is and why it was being read stays this project's own prose.

    Args:
        exc: An exception caught from ``yaml_boundary.YAML_LOAD_ERRORS``. The family includes
            builtins a constructor raises, not only ``YAMLError``, and every member renders
            through ``str`` here, so no member needs a case of its own.

    Returns:
        The message's ``repr(str(exc))`` spelling: one quoted line, with C0, DEL, and C1 controls
        escaped (named escapes such as ``\\n`` where Python defines one, ``\\xNN`` otherwise),
        literal backslashes doubled so an echoed value cannot forge an escape, and printable
        non-ASCII preserved verbatim.
    """
    return repr(str(exc))
