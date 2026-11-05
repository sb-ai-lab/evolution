"""The first-request history budget leaves ADR-0025's later folds unchanged."""

from copy import deepcopy

from evolution.tree.roles.editor.chat_editor import _compact_messages


def test_a_smaller_opening_leaves_a_tail_after_the_fold():
    def conversation(opening):
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "o" * opening},
        ]
        for k in range(8):
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": f"call-{k}",
                                "type": "function",
                                "function": {"name": "read_history", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": f"call-{k}", "content": "x" * 6000},
                ]
            )
        return messages

    small = conversation(500)
    large = conversation(16000)
    latest = deepcopy(small[-2:])
    _compact_messages(small, trigger_chars=30_000, message_max_chars=500)
    _compact_messages(large, trigger_chars=30_000, message_max_chars=500)
    assert small[-2:] == large[-2:] == latest
    assert sum(bool(m.get("tool_calls")) for m in small) >= 2
    assert sum(bool(m.get("tool_calls")) for m in large) == 1
