"""Synthetic scorer: exercise persisted subprocess input without a model."""
import json
import sys

payload = json.load(sys.stdin)
events = [event for turn in payload["view"]["history"]["turns"] for event in turn["events"]]
answers = [event["content"] for event in events if event["type"] == "assistant_message"]
print(json.dumps({"metrics": {"quality": float(answers == [payload["oracle"]["answer"]])}}))
