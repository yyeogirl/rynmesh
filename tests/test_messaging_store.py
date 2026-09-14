from __future__ import annotations

import logging

from rynmesh.services.messaging_store import MessagingStore

PEER = "G50wnVu2LhKNC5s6jUaZzlKLDXy7QlKaHgJBEiLrbNo="  # contains base64 chars

def test_append_and_history(tmp_path):
    s = MessagingStore(tmp_path)
    assert s.history(PEER) == []
    s.append(PEER, {"msg_id": "1", "dir": "out", "text": "hi"})
    s.append(PEER, {"msg_id": "2", "dir": "in", "text": "yo"})
    h = s.history(PEER)
    assert [r["msg_id"] for r in h] == ["1", "2"]
    assert h[1]["dir"] == "in"

def test_attachment_roundtrip(tmp_path):
    s = MessagingStore(tmp_path)
    s.save_attachment("msg-9", b"\x89PNGdata")
    assert s.load_attachment("msg-9") == b"\x89PNGdata"

def test_peer_id_safe_filename(tmp_path):
    s = MessagingStore(tmp_path)
    s.append("a/b=c+d", {"msg_id": "x"})        # slashes etc. must not break paths
    assert s.history("a/b=c+d")[0]["msg_id"] == "x"


def test_a_corrupt_line_does_not_take_the_conversation_down(tmp_path, caplog):
    """The log is append-only: one bad line must not hide every other message."""

    s = MessagingStore(tmp_path)
    s.append(PEER, {"msg_id": "1", "dir": "in", "text": "before"})
    path = s._conv_path(PEER)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"msg_id": "2", "text": "SECRET_TRUNCATED_MAR\n')  # a crash mid-write
        handle.write('"not an object"\n')
    s.append(PEER, {"msg_id": "3", "dir": "in", "text": "after"})

    with caplog.at_level(logging.DEBUG, logger="rynmesh.messaging_store"):
        history = s.history(PEER)
    assert [record["msg_id"] for record in history] == ["1", "3"]
    assert s.skipped_history_lines == 2

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "skipped 2 unparseable line(s)" in text
    # Neither the line nor the peer id itself reaches the log.
    assert "SECRET_TRUNCATED_MAR" not in text
    assert PEER not in text

    # And a later append still lands on the end of the same file.
    s.append(PEER, {"msg_id": "4", "dir": "in", "text": "later"})
    assert [record["msg_id"] for record in s.history(PEER)] == ["1", "3", "4"]
