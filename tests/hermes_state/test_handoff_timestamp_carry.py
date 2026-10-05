"""Compression handoffs retain durable message identity across rotations."""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "test_state.db")
    try:
        yield session_db
    finally:
        session_db.close()


class TestHandoffTimestampCarry:
    """#59661: carried handoff rows keep the durable parent row's timestamp so
    ``_display_dedupe_key`` collapses each logical message once in the lineage read."""

    @staticmethod
    def _rotate(db, parent_id, child_id, handoff):
        db.publish_compression_child(
            parent_session_id=parent_id, child_session_id=child_id, source="test",
            messages=handoff, require_compression_lease=False)

    def test_rotation_handoff_tail_appears_once_across_the_lineage(self, db):
        """The probe shape: parent tail rows re-inserted as child rows without a timestamp must
        not duplicate in the display lineage read."""
        db.create_session("root", source="test")
        for role, text in (("user", "u0"), ("assistant", "a0"), ("user", "u4"), ("assistant", "a4"),
                           ("user", "u5"), ("assistant", "a5")):
            db.append_message("root", role=role, content=text)
        handoff = [{"role": "assistant", "content": "SUMMARY"},
                   {"role": "user", "content": "u4"},
                   {"role": "assistant", "content": "a4"},
                   {"role": "user", "content": "u5"},
                   {"role": "assistant", "content": "a5"}]

        self._rotate(db, "root", "child", handoff)

        lineage = db.get_messages("child", include_ancestors=True)
        contents = [(m["role"], m["content"]) for m in lineage]
        assert len(contents) == len(set(contents)) == 7  # each logical message exactly once
        assert [m["content"] for m in lineage] == ["u0", "a0", "u4", "a4", "u5", "a5", "SUMMARY"]
        # The carry is observable on the caller's dicts: carried rows hold the parent's durable ts.
        parent_ts = {m["content"]: m["timestamp"] for m in db.get_messages("root")}
        for carried in handoff[1:]:
            assert carried["timestamp"] == parent_ts[carried["content"]]

    def test_handoff_dedupe_across_three_rotation_boundaries(self, db):
        """The same tail dicts carried root -> seg1 -> seg2 -> seg3 (reused across publishes, as
        production does) collapse to exactly one copy of each logical message in seg3's lineage."""
        db.create_session("root", source="test")
        for role, text in (("user", "u0"), ("assistant", "a0"),
                           ("user", "tail-u"), ("assistant", "tail-a")):
            db.append_message("root", role=role, content=text)
        tail = [{"role": "user", "content": "tail-u"}, {"role": "assistant", "content": "tail-a"}]
        for child_id, parent_id in (("seg1", "root"), ("seg2", "seg1"), ("seg3", "seg2")):
            handoff = [{"role": "assistant", "content": f"SUMMARY-{child_id}"}, *tail]
            self._rotate(db, parent_id, child_id, handoff)

        lineage = db.get_messages("seg3", include_ancestors=True)
        contents = [(m["role"], m["content"]) for m in lineage]
        assert len(contents) == len(set(contents)) == 7
        assert [m["content"] for m in lineage] == [
            "u0", "a0", "tail-u", "tail-a", "SUMMARY-seg1", "SUMMARY-seg2", "SUMMARY-seg3"]

    def test_distinct_same_content_turns_are_not_collapsed(self, db):
        """Repeated identical turns are DISTINCT logical messages: the identity collapse must never
        merge two turns that merely share content."""
        db.create_session("s", source="test")
        db.append_messages_batch("s", [{"role": "user", "content": "ok"}, {"role": "user", "content": "ok"}])
        # One batch: the second row's timestamp is bumped past the first (:540 monotonic bump).
        assert [m["content"] for m in db.get_messages("s", include_compacted=True)] == ["ok", "ok"]

        # Cross-boundary: a carried "ok" and a fresh post-rotation "ok" both display.
        db.create_session("root", source="test")
        db.append_message("root", role="user", content="ok")
        self._rotate(db, "root", "seg", [
            {"role": "assistant", "content": "SUMMARY"},
            {"role": "user", "content": "ok"},  # carried: adopts the root row's timestamp
        ])
        db.append_message("seg", role="user", content="ok")  # fresh turn: its own newer timestamp
        oks = [m for m in db.get_messages("seg", include_ancestors=True) if m["content"] == "ok"]
        assert len(oks) == 2

    def test_carry_parent_timestamps_does_not_overwrite_carried_timestamp(self, db):
        """Handoff dicts that already carry a timestamp are never clobbered by the carry."""
        db.create_session("root", source="test")
        db.append_message("root", role="user", content="u1")
        db.append_message("root", role="assistant", content="a1")
        stored = {m["content"]: m["timestamp"] for m in db.get_messages("root")}
        handoff = [{"role": "assistant", "content": "SUMMARY"},
                   {"role": "user", "content": "u1", "timestamp": stored["u1"]},
                   {"role": "assistant", "content": "a1", "timestamp": stored["a1"] + 500.0},
                   {"role": "user", "content": "fresh", "timestamp": 1234.5}]

        self._rotate(db, "root", "child", handoff)

        assert handoff[1]["timestamp"] == stored["u1"]
        assert handoff[2]["timestamp"] == stored["a1"] + 500.0
        assert handoff[3]["timestamp"] == 1234.5
        contents = [(m["role"], m["content"])
                    for m in db.get_messages("child", include_ancestors=True)]
        # u1 collapses (carried the durable ts); the offset a1 does not (carried ts is authoritative).
        assert contents.count(("user", "u1")) == 1
        assert contents.count(("assistant", "a1")) == 2
        assert contents.count(("user", "fresh")) == 1

    def test_archive_and_compact_carried_rows_keep_identity(self, db):
        """In-place double compaction: a row carried through both compactions keeps a stable display
        identity (same durable timestamp) and never duplicates in the display read."""
        db.create_session("s", source="test")
        for role, text in (("user", "u0"), ("assistant", "a0"),
                           ("user", "keep me"), ("assistant", "tail-a")):
            db.append_message("s", role=role, content=text)
        carried = [{"role": "user", "content": "keep me"}, {"role": "assistant", "content": "tail-a"}]
        db.archive_and_compact("s", [{"role": "assistant", "content": "S1"}, *carried], tail_count=2)
        first_ts = {m["content"]: m["timestamp"] for m in db.get_messages("s")}

        db.append_messages_batch(
            "s", [{"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}])
        db.archive_and_compact(
            "s",
            [{"role": "assistant", "content": "S2"}, *carried,
             {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"}],
            tail_count=4)

        visible = db.get_messages("s", include_compacted=True)
        contents = [(m["role"], m["content"]) for m in visible]
        assert len(contents) == len(set(contents))
        assert [m["content"] for m in visible] == ["u0", "a0", "S1", "S2", "keep me", "tail-a", "u2", "a2"]
        second_ts = {m["content"]: m["timestamp"] for m in db.get_messages("s")}
        assert second_ts["keep me"] == first_ts["keep me"]
        assert second_ts["tail-a"] == first_ts["tail-a"]
